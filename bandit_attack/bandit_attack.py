import glob
import sys
sys.path.append("/home1/machen/meta_perturbations_black_box_attack")
import argparse
import json
import os
import os.path as osp
import random
import time
from types import SimpleNamespace
from cifar_models.model_constructor import ModelConstructor
import glog as log
import numpy as np
import torch
from torch.nn import functional as F
from torch.nn.modules import Upsample

from config import IMAGE_SIZE, IN_CHANNELS, CLASS_NUM, PY_ROOT, MODELS_TEST
from dataset.dataset_loader_maker import DataLoaderMaker
from torchvision.transforms import transforms

class BanditsAttack(object):
    def __init__(self, args):
        self.dataset_loader = DataLoaderMaker.get_img_label_data_loader(args.dataset, args.batch_size, False)
        self.total_images = args.total_images
        self.query_all = torch.zeros(self.total_images)
        self.correct_all = torch.zeros_like(self.query_all)  # number of images
        self.not_done_all = torch.zeros_like(self.query_all)  # always set to 0 if the original image is misclassified
        self.success_all = torch.zeros_like(self.query_all)
        self.success_query_all = torch.zeros_like(self.query_all)
        self.not_done_loss_all = torch.zeros_like(self.query_all)
        self.not_done_prob_all = torch.zeros_like(self.query_all)
        self.normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    def norm(self, t):
        assert len(t.shape) == 4
        norm_vec = torch.sqrt(t.pow(2).sum(dim=[1, 2, 3])).view(-1, 1, 1, 1)
        norm_vec += (norm_vec == 0).float() * 1e-8
        return norm_vec

    ###
    # Different optimization steps
    # All take the form of func(x, g, lr)
    # eg: exponentiated gradients
    # l2/linf: projected gradient descent
    ###

    def eg_step(self, x, g, lr):
        real_x = (x + 1) / 2  # from [-1, 1] to [0, 1]
        pos = real_x * torch.exp(lr * g)
        neg = (1 - real_x) * torch.exp(-lr * g)
        new_x = pos / (pos + neg)
        return new_x * 2 - 1

    def linf_step(self, x, g, lr):
        return x + lr * torch.sign(g)

    def l2_prior_step(self, x, g, lr):
        new_x = x + lr * g / self.norm(g)
        norm_new_x = self.norm(new_x)
        norm_mask = (norm_new_x < 1.0).float()
        return new_x * norm_mask + (1 - norm_mask) * new_x / norm_new_x

    def gd_prior_step(self, x, g, lr):
        return x + lr * g

    def l2_image_step(self, x, g, lr):
        return x + lr * g / self.norm(g)

    ##
    # Projection steps for l2 and linf constraints:
    # All take the form of func(new_x, old_x, epsilon)
    ##
    def l2_proj(self, image, eps):
        orig = image.clone()
        def proj(new_x):
            delta = new_x - orig
            out_of_bounds_mask = (self.norm(delta) > eps).float()
            x = (orig + eps * delta / self.norm(delta)) * out_of_bounds_mask
            x += new_x * (1 - out_of_bounds_mask)
            return x
        return proj

    def linf_proj(self, image, eps):
        orig = image.clone()
        def proj(new_x):
            return orig + torch.clamp(new_x - orig, -eps, eps)
        return proj

    def xent_loss(self, logit, label, target=None):
        if target is not None:
            return -F.cross_entropy(logit, target, reduction='none')
        else:
            return F.cross_entropy(logit, label, reduction='none')

    def normalized_image(self, x):
        x_copy = x.clone()
        x_copy = torch.stack([self.normalize(x_copy[i]) for i in range(x.size(0))])
        return x_copy

    ##
    # Main functions
    ##

    def make_adversarial_examples(self, batch_index, images, true_labels, args, target_model):
        '''
        The attack process for generating adversarial examples with priors.
        '''
        prior_size = IMAGE_SIZE[args.dataset][0] if not args.tiling else args.tile_size
        assert args.tiling == (args.dataset == "ImageNet")
        if args.tiling:
            upsampler = Upsample(size=(IMAGE_SIZE[args.dataset][0], IMAGE_SIZE[args.dataset][1]))
        else:
            upsampler = lambda x: x
        with torch.no_grad():
            if args.dataset == "ImageNet":
                logit = target_model(self.normalized_image(images))
            else:
                logit = target_model(images)
        pred = logit.argmax(dim=1)
        query = torch.zeros(args.batch_size).cuda()
        correct = pred.eq(true_labels).float()  # shape = (batch_size,)
        not_done = correct.clone()  # shape = (batch_size,)
        selected = torch.arange(batch_index * args.batch_size,
                                (batch_index + 1) * args.batch_size)  # 选择这个batch的所有图片的index
        if args.targeted:
            if args.target_type == 'random':
                target_labels = torch.randint(low=0, high=CLASS_NUM[args.dataset], size=true_labels.size()).long().cuda()
            elif args.target_type == 'least_likely':
                target_labels = logit.argmin(dim=1)
        else:
            target_labels = None
        prior = torch.zeros(args.batch_size, IN_CHANNELS[args.dataset], prior_size, prior_size).cuda()
        dim = prior.nelement() / args.batch_size               # nelement() --> total number of elements
        prior_step = self.gd_prior_step if args.norm == 'l2' else self.eg_step
        image_step = self.l2_image_step if args.norm == 'l2' else self.linf_step
        proj_maker = self.l2_proj if args.norm == 'l2' else self.linf_proj  # 调用proj_maker返回的是一个函数
        proj_step = proj_maker(images, args.epsilon)
        # Loss function
        adv_images = images.clone()
        not_success_images = None
        for step_index in range(args.max_queries // 2):
            # Create noise for exporation, estimate the gradient, and take a PGD step
            exp_noise = args.exploration * torch.randn_like(prior) / (dim ** 0.5)  # parameterizes the exploration to be done around the prior
            # Query deltas for finite difference estimator
            exp_noise = exp_noise.cuda()
            q1 = upsampler(prior + exp_noise)  # 这就是Finite Difference算法， prior相当于论文里的v，这个prior也会更新，把梯度累积上去
            q2 = upsampler(prior - exp_noise)   # prior 相当于累积的更新量，用这个更新量，再去修改image，就会变得非常准
            # Loss points for finite difference estimator

            q1_images = adv_images + args.fd_eta * q1 / self.norm(q1)
            q2_images = adv_images + args.fd_eta * q2 / self.norm(q2)
            with torch.no_grad():
                if args.dataset == "ImageNet":
                    q1_logits = target_model(self.normalized_image(q1_images))
                    q2_logits = target_model(self.normalized_image(q2_images))
                else:
                    q1_logits = target_model(q1_images)
                    q2_logits = target_model(q2_images)
            l1 = self.xent_loss(q1_logits, true_labels, target_labels)
            l2 = self.xent_loss(q2_logits, true_labels, target_labels)
            # Finite differences estimate of directional derivative
            est_deriv = (l1 - l2) / (args.fd_eta * args.exploration)  # 方向导数 , l1和l2是loss
            # 2-query gradient estimate
            est_grad = est_deriv.view(-1, 1, 1, 1) * exp_noise  # B, C, H, W,
            # Update the prior with the estimated gradient
            prior = prior_step(prior, est_grad, args.online_lr)  # 注意，修正的是prior,这就是bandit算法的精髓
            grad = upsampler(prior)  # prior相当于梯度
            ## Update the image:
            # take a pgd step using the prior
            adv_images = image_step(adv_images, grad * correct.view(-1, 1, 1, 1), args.image_lr)  # prior放大后相当于累积的更新量，可以用来更新
            adv_images = proj_step(adv_images)
            adv_images = torch.clamp(adv_images, 0, 1)
            with torch.no_grad():
                if args.dataset == "ImageNet":
                    adv_logit = target_model(self.normalized_image(adv_images))
                else:
                    adv_logit = target_model(adv_images)
            adv_pred = adv_logit.argmax(dim=1)
            adv_prob = F.softmax(adv_logit, dim=1)
            adv_loss = self.xent_loss(adv_logit, true_labels, target_labels)
            ## Continue query count
            query = query + 2 * not_done
            if args.targeted:
                not_done = not_done * (1 - adv_pred.eq(target_labels)).float()  # not_done初始化为 correct, shape = (batch_size,)
            else:
                not_done = not_done * adv_pred.eq(true_labels).float()  # 只要是跟原始label相等的，就还需要query，还没有成功
            success = (1 - not_done) * correct
            success_query = success * query
            not_done_loss = adv_loss * not_done
            not_done_prob = adv_prob[torch.arange(args.batch_size), true_labels] * not_done

            log.info('Attacking image {} - {} / {}, step {}, max query {}'.format(
                batch_index * args.batch_size, (batch_index + 1) * args.batch_size,
                self.total_images, step_index + 1, int(query.max().item())
            ))
            log.info('        correct: {:.4f}'.format(correct.mean().item()))
            log.info('       not_done: {:.4f}'.format(not_done.mean().item()))
            log.info('      fd_scalar: {:.9f}'.format((l1 - l2).mean().item()))
            if success.sum().item() > 0:
                log.info('     mean_query: {:.4f}'.format(success_query[success.byte()].mean().item()))
                log.info('   median_query: {:.4f}'.format(success_query[success.byte()].median().item()))
            if not_done.sum().item() > 0:
                log.info('  not_done_loss: {:.4f}'.format(not_done_loss[not_done.byte()].mean().item()))
                log.info('  not_done_prob: {:.4f}'.format(not_done_prob[not_done.byte()].mean().item()))

            if not not_done.byte().any(): # all success
                break
        else:
            not_success_images = images[not_done.byte()].detach().cpu()

        for key in ['query', 'correct',  'not_done',
                    'success', 'success_query', 'not_done_loss', 'not_done_prob']:
            value_all = getattr(self, key+"_all")
            value = eval(key)
            value_all[selected] = value.detach().float().cpu()  # 由于value_all是全部图片都放在一个数组里，当前batch选择出来

        return not_success_images

    def attack_all_images(self, args, target_model, result_dump_path):

        not_success_images_list = []
        for batch_idx, (images, true_labels) in enumerate(self.dataset_loader):
            if batch_idx * args.batch_size >= self.total_images:
                break
            not_success_images = self.make_adversarial_examples(batch_idx, images.cuda(), true_labels.cuda(), args, target_model)
            if not_success_images is not None:
                not_success_images_list.append(not_success_images)

        all_not_success_images = torch.cat(not_success_images_list, 0).detach().cpu().numpy()

        log.info('Attack finished ({} images)'.format(self.total_images))
        log.info('        avg correct: {:.4f}'.format(self.correct_all.mean().item()))
        log.info('       avg not_done: {:.4f}'.format(self.not_done_all.mean().item()))  # 有多少图没做完
        if self.success_all.sum().item() > 0:
            log.info('     avg mean_query: {:.4f}'.format(self.success_query_all[self.success_all.byte()].mean().item()))
            log.info('   avg median_query: {:.4f}'.format(self.success_query_all[self.success_all.byte()].median().item()))
            log.info('     max query: {}'.format(self.success_query_all[self.success_all.byte()].max().item()))
        if self.not_done_all.sum().item() > 0:
            log.info('  avg not_done_loss: {:.4f}'.format(self.not_done_loss_all[self.not_done_all.byte()].mean().item()))
            log.info('  avg not_done_prob: {:.4f}'.format(self.not_done_prob_all[self.not_done_all.byte()].mean().item()))
        log.info('Saving results to {}'.format(result_dump_path))
        meta_info_dict = {"avg_correct": self.correct_all.mean().item(),"adv_not_done": self.not_done_all.mean().item(),
                          "mean_query": self.success_query_all[self.success_all.byte()].mean().item(),
                          "median_query": self.success_query_all[self.success_all.byte()].median().item(),
                          "max_query": self.success_query_all[self.success_all.byte()].max().item(),
                          "not_done_loss": self.not_done_loss_all[self.not_done_all.byte()].mean().item(),
                          "not_done_prob": self.not_done_prob_all[self.not_done_all.byte()].mean().item()}
        meta_info_dict['args'] = vars(args)
        with open(result_dump_path, "w") as result_file_obj:
            json.dump(meta_info_dict, result_file_obj, indent=4, sort_keys=True)
        save_npy_path = os.path.dirname(result_dump_path) + "/not_done_images.npy"
        np.save(save_npy_path, all_not_success_images)
        log.info("done, write stats info to {}".format(result_dump_path))



def get_exp_dir_name(dataset, norm, targeted, target_type):
    import string
    from datetime import datetime
    dirname = datetime.now().strftime('%Y_%m_%d_%H_%M_%S')
    target_str = "untargeted" if not targeted else "targeted_{}".format(target_type)
    dirname = 'bandits_attack-{}-{}-{}-'.format(dataset, norm, target_str) + dirname
    return dirname

def print_args(args):
    keys = sorted(vars(args).keys())
    max_len = max([len(key) for key in keys])
    for key in keys:
        prefix = ' ' * (max_len + 1 - len(key)) + key
        log.info('{:s}: {}'.format(prefix, args.__getattribute__(key)))

def set_log_file(fname):
    # set log file
    # simple tricks for duplicating logging destination in the logging module such as:
    # logging.getLogger().addHandler(logging.FileHandler(filename))
    # does NOT work well here, because python Traceback message (not via logging module) is not sent to the file,
    # the following solution (copied from : https://stackoverflow.com/questions/616645) is a little bit
    # complicated but simulates exactly the "tee" command in linux shell, and it redirects everything
    import subprocess
    # sys.stdout = os.fdopen(sys.stdout.fileno(), 'wb', 0)
    tee = subprocess.Popen(['tee', fname], stdin=subprocess.PIPE)
    os.dup2(tee.stdin.fileno(), sys.stdout.fileno())
    os.dup2(tee.stdin.fileno(), sys.stderr.fileno())

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu",type=int, required=True)
    parser.add_argument('--max-queries', type=int, default=10000)
    parser.add_argument('--fd-eta', type=float, help='\eta, used to estimate the derivative via finite differences')
    parser.add_argument('--image-lr', type=float, help='Learning rate for the image (iterative attack)')
    parser.add_argument('--online-lr', type=float, help='Learning rate for the prior')
    parser.add_argument('--norm', type=str, help='Which lp constraint to run bandits [linf|l2]')
    parser.add_argument('--exploration', type=float,
                        help='\delta, parameterizes the exploration to be done around the prior')
    parser.add_argument('--tile-size', type=int, help='the side length of each tile (for the tiling prior)')
    parser.add_argument('--tiling', action='store_true')
    parser.add_argument('--json-config', type=str, default='/home1/machen/meta_perturbations_black_box_attack/bandits_attack_conf.json',
                        help='a config file to be passed in instead of arguments')
    parser.add_argument('--epsilon', type=float, help='the lp perturbation bound')
    parser.add_argument('--batch-size', type=int, help='batch size for bandits')
    parser.add_argument('--dataset', type=str, required=True,
                        choices=['CIFAR-10', 'ImageNet', "FashionMNIST", "MNIST", "TinyImageNet"],
                        help='which dataset to use')
    parser.add_argument('--arch', default='wideresnet28drop', type=str, help='network architecture')
    parser.add_argument('--test-archs', action="store_true")
    parser.add_argument("--total-images",type=int)
    parser.add_argument('--targeted', action="store_true")
    parser.add_argument('--target_type',type=str, default='random', choices=["random", "least_likely"])
    parser.add_argument('--exp-dir', default='logs', type=str,
                        help='directory to save results and logs')
    parser.add_argument('--seed', default=0, type=int, help='random seed')

    args = parser.parse_args()
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    os.environ['CUDA_VISIBLE_DEVICES'] = str(args.gpu)
    os.environ['CUDA_VISIBLE_DEVICE'] = str(args.gpu)
    print("using GPU {}".format(args.gpu))

    args_dict = None
    if not args.json_config:
        # If there is no json file, all of the args must be given
        args_dict = vars(args)
    else:
        # If a json file is given, use the JSON file as the base, and then update it with args
        defaults = json.load(open(args.json_config))[args.dataset][args.norm]
        arg_vars = vars(args)
        arg_vars = {k: arg_vars[k] for k in arg_vars if arg_vars[k] is not None}
        defaults.update(arg_vars)
        args = SimpleNamespace(**defaults)
        args_dict = defaults
    if args.targeted:
        args.max_queries = 50000
    args.exp_dir = osp.join(args.exp_dir, get_exp_dir_name(args.dataset, args.norm, args.targeted, args.target_type))  # 随机产生一个目录用于实验
    os.makedirs(args.exp_dir, exist_ok=True)
    set_log_file(osp.join(args.exp_dir, 'run.log'))
    log.info('Command line is: {}'.format(' '.join(sys.argv)))
    log.info("Log file is written in {}".format(osp.join(args.exp_dir, 'run.log')))
    log.info('Called with args:')
    print_args(args)
    torch.backends.cudnn.deterministic = True
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    archs = [args.arch]
    if args.test_archs:
        archs = MODELS_TEST
    for arch in archs:
        test_model_list_path = "{}/train_pytorch_model/real_image_model/{}@{}@*.pth.tar".format(PY_ROOT, args.dataset,
                                                                                           arch)
        test_model_list_path = list(glob.glob(test_model_list_path))
        if len(test_model_list_path) == 0:  # this arch does not exists in args.dataset
            continue

        if args.dataset in ["CIFAR-10","MNIST","FashionMNIST"]:
            target_model = ModelConstructor.construct_cifar_model(arch, args.dataset)
        elif args.dataset == "ImageNet":
            target_model = ModelConstructor.construct_imagenet_model(arch)
        elif args.dataset == "TinyImageNet":
            target_model = ModelConstructor.construct_tiny_imagenet_model(arch, args.dataset)
        target_model.eval()

        if args.dataset != "ImageNet":
            model_list_path = "{}/train_pytorch_model/real_image_model/{}@{}@*.pth.tar".format(PY_ROOT, args.dataset,
                                                                                               arch)
            model_path = list(glob.glob(model_list_path))[0]
            target_model.load_state_dict(
                torch.load(model_path, map_location=lambda storage, location: storage)["state_dict"])
        target_model.cuda()
        log.info("initializing target model {} on {}".format(arch, args.dataset))

        attacker = BanditsAttack(args)
        save_result_path = args.exp_dir + "/{}_result.json".format(arch)
        attacker.attack_all_images(args, target_model, save_result_path)

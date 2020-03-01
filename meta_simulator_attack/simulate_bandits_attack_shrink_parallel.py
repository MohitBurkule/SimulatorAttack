import glob
import sys
from collections import deque, OrderedDict, defaultdict

sys.path.append("/home1/machen/meta_perturbations_black_box_attack")
import argparse
import json
import os
import os.path as osp
import random
from types import SimpleNamespace
from dataset.model_constructor import StandardModel
import glog as log
import numpy as np
import torch
from torch.nn import functional as F
from torch.nn.modules import Upsample
from config import IN_CHANNELS, CLASS_NUM, PY_ROOT, MODELS_TEST_STANDARD
from dataset.dataset_loader_maker import DataLoaderMaker
from utils.statistics_toolkit import success_rate_and_query_coorelation, success_rate_avg_query
from meta_simulator_attack.meta_model_finetune import MetaModelFinetune, MemoryEfficientMetaModelFinetune
import torch.multiprocessing as mp

class FinetuneQueue(object):
    def __init__(self, batch_size, meta_seq_len, img_idx_to_batch_idx):
        self.img_idx_to_batch_idx = img_idx_to_batch_idx
        self.q1_images_for_finetune = {}
        self.q2_images_for_finetune = {}
        self.q1_logits_for_finetune = {}
        self.q2_logits_for_finetune = {}
        for batch_idx in range(batch_size):
            self.q1_images_for_finetune[batch_idx] = deque(maxlen=meta_seq_len)
            self.q2_images_for_finetune[batch_idx] = deque(maxlen=meta_seq_len)
            self.q1_logits_for_finetune[batch_idx] = deque(maxlen=meta_seq_len)
            self.q2_logits_for_finetune[batch_idx] = deque(maxlen=meta_seq_len)

    def append(self, q1_images, q2_images, q1_logits, q2_logits):
        for img_idx, (q1_image, q2_image, q1_logit, q2_logit) in enumerate(zip(q1_images, q2_images, q1_logits, q2_logits)):
            batch_idx = self.img_idx_to_batch_idx[img_idx]
            self.q1_images_for_finetune[batch_idx].append(q1_image)
            self.q2_images_for_finetune[batch_idx].append(q2_image)
            self.q1_logits_for_finetune[batch_idx].append(q1_logit)
            self.q2_logits_for_finetune[batch_idx].append(q2_logit)

    def stack_history_track(self):
        q1_images = []
        q2_images = []
        q1_logits = []
        q2_logits = []
        for img_idx, batch_idx in sorted(self.img_idx_to_batch_idx.proj_dict.items(), key=lambda e:e[0]):
            q1_images.append(torch.stack(list(self.q1_images_for_finetune[batch_idx])))  # T,C,H,W
            q2_images.append(torch.stack(list(self.q2_images_for_finetune[batch_idx])))  # T,C,H,W
            q1_logits.append(torch.stack(list(self.q1_logits_for_finetune[batch_idx])))  # T, classes
            q2_logits.append(torch.stack(list(self.q2_logits_for_finetune[batch_idx])))  #  T, classes
        q1_images = torch.stack(q1_images)
        q2_images = torch.stack(q2_images)
        q1_logits = torch.stack(q1_logits)
        q2_logits = torch.stack(q2_logits)
        return q1_images, q2_images, q1_logits, q2_logits

class ImageIdxToOrigBatchIdx(object):
    def __init__(self, batch_size):
        self.proj_dict = OrderedDict()
        for img_idx in range(batch_size):
            self.proj_dict[img_idx] = img_idx

    def __delitem__(self, del_img_idx):
        del self.proj_dict[del_img_idx]
        all_key_value = sorted(list(self.proj_dict.items()), key=lambda e:e[0])
        for seq_idx, (img_idx, batch_idx) in enumerate(all_key_value):
            del self.proj_dict[img_idx]
            self.proj_dict[seq_idx] = batch_idx

    def __getitem__(self, img_idx):
        return self.proj_dict[img_idx]


# 更简单的方案，1000张图，分成100张一组的10组，每组用一个模型来跑，还可以多卡并行
class SimulateBanditsAttackShrink(object):
    def __init__(self, args):
        self.tot_skip_indexes = None
        if args.bandit_file and os.path.exists(args.bandit_file):
            with open(args.bandit_file, "r") as file_obj:
                json_data = json.load(file_obj)
                self.tot_skip_indexes = np.array(json_data["not_done_all"]).astype(np.int32)  # 出现1了表示以前就fail，跳过去
        self.sub_dataset_loader = {}
        self.sub_skip_indexes = {}
        self.subset_pos = {}
        for proc_id in range(args.proc_num):
            self.sub_dataset_loader[proc_id] = DataLoaderMaker.get_test_attacked_subdata(args.dataset,
                                                                        args.batch_size, proc_id, args.proc_num)
            orig_total_images_num = self.sub_dataset_loader[proc_id].dataset.orig_dataset_len
            subset_pos = self.sub_dataset_loader[proc_id].dataset.subset_pos
            self.subset_pos[proc_id] = subset_pos
            if self.tot_skip_indexes is not None:
                self.sub_skip_indexes[proc_id] = self.tot_skip_indexes[min(subset_pos): max(subset_pos)+1]
            else:
                self.sub_skip_indexes[proc_id] = np.zeros(len(subset_pos)).astype(np.int32)

        # 以下为全局变量
        self.query_all = torch.zeros(orig_total_images_num)
        self.correct_all = torch.zeros_like(self.query_all)  # number of images
        self.not_done_all = torch.zeros_like(self.query_all)  # always set to 0 if the original image is misclassified
        self.success_all = torch.zeros_like(self.query_all)
        self.success_query_all = torch.zeros_like(self.query_all)
        self.not_done_loss_all = torch.zeros_like(self.query_all)
        self.not_done_prob_all = torch.zeros_like(self.query_all)

    def chunks(self, l, each_slice_len):
        each_slice_len = max(1, each_slice_len)
        return list(l[i:i + each_slice_len] for i in range(0, len(l), each_slice_len))

    def norm(self, t):
        assert len(t.shape) == 4
        norm_vec = torch.sqrt(t.pow(2).sum(dim=[1, 2, 3])).view(-1, 1, 1, 1)
        norm_vec += (norm_vec == 0).float() * 1e-8
        return norm_vec

    def eg_step(self, x, g, lr):
        real_x = (x + 1) / 2  # from [-1, 1] to [0, 1]
        pos = real_x * torch.exp(lr * g)
        neg = (1 - real_x) * torch.exp(-lr * g)
        new_x = pos / (pos + neg)
        return new_x * 2 - 1

    def linf_step(self, x, g, lr):
        return x + lr * torch.sign(g)

    def linf_proj_step(self, image, epsilon, adv_image):
        return image + torch.clamp(adv_image - image, -epsilon, epsilon)

    def l2_proj_step(self, image, epsilon, adv_image):
        delta = adv_image - image
        out_of_bounds_mask = (self.norm(delta) > epsilon).float()
        return out_of_bounds_mask * (image + epsilon * delta / self.norm(delta)) + (1 - out_of_bounds_mask) * adv_image

    def gd_prior_step(self, x, g, lr):
        return x + lr * g

    def l2_image_step(self, x, g, lr):
        return x + lr * g / self.norm(g)


    def xent_loss(self, logit, label, target=None):
        if target is not None:
            return -F.cross_entropy(logit, target, reduction='none')
        else:
            return F.cross_entropy(logit, label, reduction='none')

    def cw_loss(self, logit, label, target=None):
        if target is not None:
            # targeted cw loss: logit_t - max_{i\neq t}logit_i
            _, argsort = logit.sort(dim=1, descending=True)
            target_is_max = argsort[:, 0].eq(target).long()
            second_max_index = target_is_max.long() * argsort[:, 1] + (1 - target_is_max).long() * argsort[:, 0]
            target_logit = logit[torch.arange(logit.shape[0]), target]
            second_max_logit = logit[torch.arange(logit.shape[0]), second_max_index]
            return target_logit - second_max_logit
        else:
            # untargeted cw loss: max_{i\neq y}logit_i - logit_y
            _, argsort = logit.sort(dim=1, descending=True)
            gt_is_max = argsort[:, 0].eq(label).long()
            second_max_index = gt_is_max.long() * argsort[:, 1] + (1 - gt_is_max).long() * argsort[:, 0]
            gt_logit = logit[torch.arange(logit.shape[0]), label]
            second_max_logit = logit[torch.arange(logit.shape[0]), second_max_index]
            return second_max_logit - gt_logit

    def delete_tensor_by_index(self, del_index,  *tensors):
        return_tensors = []
        for tensor in tensors:
            tensor = torch.cat([tensor[0:del_index], tensor[del_index + 1:]], 0)
            return_tensors.append(tensor)
        return return_tensors



    def attack_sub_process(self, args, arch, subdata_loader, sub_skip_indexes, subset_pos):
        # subset_pos用于回调函数汇报汇总统计结果
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ['CUDA_VISIBLE_DEVICES'] = str(args.gpu)
        os.environ["TORCH_HOME"] = "/home1/machen/.cache/torch/pretrainedmodels"
        model = StandardModel(args.dataset, arch, no_grad=True)
        model.cuda()
        model.eval()
        # 带有缩减功能的，攻击成功的图片自动删除掉
        meta_finetuner = MemoryEfficientMetaModelFinetune(args.dataset, args.batch_size, args.meta_train_type, args.distillation_loss,
                                           args.data_loss, args.norm, args.data_loss == "xent")
        sub_skip_indexes = self.chunks(sub_skip_indexes, args.batch_size)
        subset_total_images = len(subdata_loader.dataset.subset_pos)
        for data_idx, data_tuple in enumerate(subdata_loader):
            if args.dataset == "ImageNet":
                if model.input_size[-1] >= 299:
                    images, true_labels = data_tuple[1], data_tuple[2]
                else:
                    images, true_labels = data_tuple[0], data_tuple[2]
            else:
                images, true_labels = data_tuple[0], data_tuple[1]  # FIXME 缩减
            if images.size(-1) != model.input_size[-1]:
                images = F.interpolate(images, size=model.input_size[-1], mode='bilinear',align_corners=True)
            skip_batch_index_list = np.nonzero(np.asarray(sub_skip_indexes[data_idx]))[0].tolist()
            img_idx_to_batch_idx = ImageIdxToOrigBatchIdx(args.batch_size)
            if len(skip_batch_index_list) > 0:
                for skip_index in skip_batch_index_list:
                    images, true_labels = self.delete_tensor_by_index(skip_index, images, true_labels)
                    del img_idx_to_batch_idx[skip_index]

            first_finetune = True
            finetune_queue = FinetuneQueue(args.batch_size, args.meta_seq_len, img_idx_to_batch_idx)

            prior_size = model.input_size[-1] if not args.tiling else args.tile_size
            assert args.tiling == (args.dataset == "ImageNet")
            if args.tiling:
                upsampler = Upsample(size=(model.input_size[-2], model.input_size[-1]))
            else:
                upsampler = lambda x: x
            with torch.no_grad():
                logit = model(images)
            pred = logit.argmax(dim=1)
            query = torch.zeros(images.size(0)).cuda()  # FIXME 缩减
            correct = pred.eq(true_labels).float()  # shape = (batch_size,)  # FIXME 缩减
            not_done = correct.clone()  # shape = (batch_size,)  # FIXME 缩减
            if args.targeted:
                if args.target_type == 'random':
                    target_labels = torch.randint(low=0, high=CLASS_NUM[args.dataset],
                                                  size=true_labels.size()).long().cuda()
                    invalid_target_index = target_labels.eq(true_labels)
                    while invalid_target_index.sum().item() > 0:
                        target_labels[invalid_target_index] = torch.randint(low=0, high=logit.shape[1],
                                                                            size=target_labels[
                                                                                invalid_target_index].shape).long().cuda()
                        invalid_target_index = target_labels.eq(true_labels)
                elif args.target_type == 'least_likely':
                    target_labels = logit.argmin(dim=1)
                elif args.target_type == "increment":
                    target_labels = torch.fmod(true_labels + 1, CLASS_NUM[args.dataset])  # FIXME 缩减
                else:
                    raise NotImplementedError('Unknown target_type: {}'.format(args.target_type))
            else:
                target_labels = None
            prior = torch.zeros(images.size(0), IN_CHANNELS[args.dataset], prior_size, prior_size).cuda()  # FIXME 缩减
            prior_step = self.gd_prior_step if args.norm == 'l2' else self.eg_step
            image_step = self.l2_image_step if args.norm == 'l2' else self.linf_step
            proj_step = self.l2_proj_step if args.norm == 'l2' else self.linf_proj_step  # 调用proj_maker返回的是一个函数
            criterion = self.cw_loss if args.data_loss == "cw" else self.xent_loss
            adv_images = images.clone()
            for step_index in range(1, args.max_queries // 2 + 1):
                # Create noise for exporation, estimate the gradient, and take a PGD step
                dim = prior.nelement() / images.size(0)  # nelement() --> total number of elements
                exp_noise = args.exploration * torch.randn_like(prior) / (dim ** 0.5)  # parameterizes the exploration to be done around the prior
                exp_noise = exp_noise.cuda()
                q1 = upsampler(prior + exp_noise)  # 这就是Finite Difference算法， prior相当于论文里的v，这个prior也会更新，把梯度累积上去
                q2 = upsampler(prior - exp_noise)  # prior 相当于累积的更新量，用这个更新量，再去修改image，就会变得非常准
                # Loss points for finite difference estimator
                q1_images = adv_images + args.fd_eta * q1 / self.norm(q1)
                q2_images = adv_images + args.fd_eta * q2 / self.norm(q2)
                predict_by_target_model = False
                use_target_threshold = 0.11 if args.norm == "l2" else 0.21
                if (step_index <= args.warm_up_steps or (
                        step_index - args.warm_up_steps) % args.meta_predict_steps == 0) \
                        or len(not_done) / float(args.batch_size) <= use_target_threshold:
                    log.info("predict from target model")
                    predict_by_target_model = True
                    with torch.no_grad():
                        q1_logits = model(q1_images)
                        q2_logits = model(q2_images)
                    finetune_queue.append(q1_images.detach(), q2_images.detach(), q1_logits.detach(), q2_logits.detach())

                    if step_index >= args.warm_up_steps and len(not_done) / float(args.batch_size) > use_target_threshold:
                        q1_images_seq, q2_images_seq, q1_logits_seq, q2_logits_seq = finetune_queue.stack_history_track()
                        finetune_times = args.finetune_times if first_finetune else random.randint(1, 3)
                        meta_finetuner.finetune(q1_images_seq, q2_images_seq, q1_logits_seq, q2_logits_seq,
                                                     finetune_times,
                                                     first_finetune, img_idx_to_batch_idx)
                        first_finetune = False
                else:
                    with torch.no_grad():
                        q1_logits, q2_logits = meta_finetuner.predict(q1_images, q2_images, img_idx_to_batch_idx)

                l1 = criterion(q1_logits, true_labels, target_labels)
                l2 = criterion(q2_logits, true_labels, target_labels)
                # Finite differences estimate of directional derivative
                est_deriv = (l1 - l2) / (args.fd_eta * args.exploration)  # 方向导数 , l1和l2是loss
                # 2-query gradient estimate
                est_grad = est_deriv.view(-1, 1, 1, 1) * exp_noise  # B, C, H, W,
                # Update the prior with the estimated gradient
                prior = prior_step(prior, est_grad, args.online_lr)  # 注意，修正的是prior,这就是bandit算法的精髓
                grad = upsampler(prior)  # prior相当于梯度
                ## Update the image:
                adv_images = image_step(adv_images, grad * correct.view(-1, 1, 1, 1),
                                        args.image_lr)  # prior放大后相当于累积的更新量，可以用来更新
                adv_images = proj_step(adv_images)
                adv_images = torch.clamp(adv_images, 0, 1)

                with torch.no_grad():
                    adv_logit = model(adv_images)  #
                adv_pred = adv_logit.argmax(dim=1)
                adv_prob = F.softmax(adv_logit, dim=1)
                adv_loss = criterion(adv_logit, true_labels, target_labels)
                ## Continue query count
                if predict_by_target_model:
                    query = query + 2 * not_done # FIXME 注意query和not_done,这几个变量都是缩减过的
                if args.targeted:
                    not_done = not_done * (
                                1 - adv_pred.eq(target_labels)).float()  # not_done初始化为 correct, shape = (batch_size,)
                else:
                    not_done = not_done * adv_pred.eq(true_labels).float()  # 只要是跟原始label相等的，就还需要query，还没有成功
                success = (1 - not_done) * correct
                success_query = success * query
                not_done_loss = adv_loss * not_done
                not_done_prob = adv_prob[torch.arange(adv_images.size(0)), true_labels] * not_done  # FIXME 每次缩减都要填写到all_的全局变量里汇报出去

                log.info('Attacking image {} - {} / {}, step {}, max query {}'.format(
                    data_idx * args.batch_size, (data_idx + 1) * args.batch_size,
                    subset_total_images, step_index, int(query.max().item())
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

                not_done_np = not_done.detach().cpu().numpy().astype(np.int32)
                done_img_idx_list = np.where(not_done_np == 0)[0].tolist()
                if done_img_idx_list:
                    for skip_index in done_img_idx_list:
                        images, adv_images, query, true_labels, target_labels, correct, not_done  = self.delete_tensor_by_index(skip_index,
                                                                                      images, adv_images, query,
                                                                                      true_labels, target_labels,
                                                                                      correct, not_done)
                        del img_idx_to_batch_idx[skip_index]
                else:
                    break

            # report to outer

            for key in ['query', 'correct', 'not_done',
                        'success', 'success_query', 'not_done_loss', 'not_done_prob']:
                value_all = getattr(self, key + "_all")
                value = eval(key)
                value_all[selected] = value.detach().float().cpu()  # 由于value_all是全部图片都放在一个数组里，当前batch选择出来




    def attack_all_images(self, args, arch_name, target_model, result_dump_path):
        pool = mp.Pool(processes=args.proc_num)
        for proc_id in range(args.proc_num):
            pool


        for batch_idx, data_tuple in enumerate(self.dataset_loader):
            if args.dataset == "ImageNet":
                if target_model.input_size[-1] >= 299:
                    images, true_labels = data_tuple[1], data_tuple[2]
                else:
                    images, true_labels = data_tuple[0], data_tuple[2]
            else:
                images, true_labels = data_tuple[0], data_tuple[1]
            if images.size(-1) != target_model.input_size[-1]:
                images = F.interpolate(images, size=target_model.input_size[-1], mode='bilinear',align_corners=True)

            self.make_adversarial_examples(batch_idx, images.cuda(), true_labels.cuda(), args, target_model)

        query_all_ = self.query_all.detach().cpu().numpy().astype(np.int32)
        not_done_all_ = self.not_done_all.detach().cpu().numpy().astype(np.int32)
        query_threshold_success_rate, query_success_rate = success_rate_and_query_coorelation(query_all_, not_done_all_)
        success_rate_to_avg_query = success_rate_avg_query(query_all_, not_done_all_)
        log.info('{} is attacked finished ({} images)'.format(arch_name, self.total_images))
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
        meta_info_dict = {"avg_correct": self.correct_all.mean().item(),
                          "avg_not_done": self.not_done_all.mean().item(),
                          "mean_query": self.success_query_all[self.success_all.byte()].mean().item(),
                          "median_query": self.success_query_all[self.success_all.byte()].median().item(),
                          "max_query": self.success_query_all[self.success_all.byte()].max().item(),
                          "correct_all": self.correct_all.detach().cpu().numpy().astype(np.int32).tolist(),
                          "not_done_all": self.not_done_all.detach().cpu().numpy().astype(np.int32).tolist(),
                          "query_all": self.query_all.detach().cpu().numpy().astype(np.int32).tolist(),
                          "query_threshold_success_rate_dict": query_threshold_success_rate,
                          "success_rate_to_avg_query": success_rate_to_avg_query,
                          "query_success_rate_dict": query_success_rate,
                          "not_done_loss": self.not_done_loss_all[self.not_done_all.byte()].mean().item(),
                          "not_done_prob": self.not_done_prob_all[self.not_done_all.byte()].mean().item(),
                          "args":vars(args)}
        with open(result_dump_path, "w") as result_file_obj:
            json.dump(meta_info_dict, result_file_obj, sort_keys=True)
        log.info("done, write stats info to {}".format(result_dump_path))
        self.query_all.fill_(0)
        self.correct_all.fill_(0)
        self.not_done_all.fill_(0)
        self.success_all.fill_(0)
        self.success_query_all.fill_(0)
        self.not_done_loss_all.fill_(0)
        self.not_done_prob_all.fill_(0)


def get_exp_dir_name(dataset, loss, norm, targeted, target_type, distillation_loss):
    target_str = "untargeted" if not targeted else "targeted_{}".format(target_type)
    dirname = 'simulate_bandits_attack-{}-{}_loss-{}-{}-{}'.format(dataset, loss, norm, target_str, distillation_loss)
    return dirname

def print_args(args):
    keys = sorted(vars(args).keys())
    max_len = max([len(key) for key in keys])
    for key in keys:
        prefix = ' ' * (max_len + 1 - len(key)) + key
        log.info('{:s}: {}'.format(prefix, args.__getattribute__(key)))

def set_log_file(fname):
    # the following solution (copied from : https://stackoverflow.com/questions/616645) is a little bit
    # complicated but simulates exactly the "tee" command in linux shell, and it redirects everything
    import subprocess
    # sys.stdout = os.fdopen(sys.stdout.fileno(), 'wb', 0)
    tee = subprocess.Popen(['tee', fname], stdin=subprocess.PIPE)
    os.dup2(tee.stdin.fileno(), sys.stdout.fileno())
    os.dup2(tee.stdin.fileno(), sys.stderr.fileno())

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpu",type=str, required=True)
    parser.add_argument('--max-queries', type=int, default=10000)
    parser.add_argument('--fd-eta', type=float, help='\eta, used to estimate the derivative via finite differences')
    parser.add_argument('--image-lr', type=float, help='Learning rate for the image (iterative attack)')
    parser.add_argument('--online-lr', type=float, help='Learning rate for the prior')
    parser.add_argument('--norm', type=str, required=True, help='Which lp constraint to run bandits [linf|l2]')
    parser.add_argument('--exploration', type=float,
                        help='\delta, parameterizes the exploration to be done around the prior')
    parser.add_argument('--tile-size', type=int, help='the side length of each tile (for the tiling prior)')
    parser.add_argument('--tiling', action='store_true')
    parser.add_argument('--json-config', type=str, default='/home1/machen/meta_perturbations_black_box_attack/configures/bandits_attack_conf.json',
                        help='a configures file to be passed in instead of arguments')
    parser.add_argument('--epsilon', type=float, help='the lp perturbation bound')
    parser.add_argument('--batch-size', type=int, help='batch size for bandits attack.')
    parser.add_argument("--bandit_file",type=str, default=None, help="The bandit processed file that already processced")
    parser.add_argument('--dataset', type=str, required=True,
                        choices=['CIFAR-10', 'CIFAR-100', 'ImageNet', "FashionMNIST", "MNIST", "TinyImageNet"],
                        help='which dataset to use')
    parser.add_argument('--arch', default=None, type=str, help='network architecture')
    parser.add_argument('--test_archs', action="store_true")
    parser.add_argument('--targeted', action="store_true")
    parser.add_argument('--target_type',type=str, default='increment', choices=['random', 'least_likely',"increment"])
    parser.add_argument('--exp-dir', default='logs', type=str,
                        help='directory to save results and logs')
    # meta-learning arguments
    parser.add_argument("--meta_train_type", type=str, default="2q_distillation",
                        choices=["logits_distillation", "2q_distillation"])
    parser.add_argument("--data_loss", type=str, required=True, choices=["xent", "cw"])
    parser.add_argument("--distillation_loss", type=str, required=True, choices=["mse", "pair_mse"])
    parser.add_argument("--finetune_times", type=int, default=20)
    parser.add_argument('--seed', default=1398, type=int, help='random seed')
    parser.add_argument("--meta_predict_steps", type=int, default=40)
    parser.add_argument("--warm_up_steps", type=int, default=20)
    parser.add_argument("--meta_seq_len", type=int, default=20)

    args = parser.parse_args()

    print("using GPU {}".format(args.gpu))
    gpu_num = len(args.gpu.split(","))
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
        if args.dataset == "ImageNet":
            args.max_queries = 50000
    args.exp_dir = osp.join(args.exp_dir, get_exp_dir_name(args.dataset, args.data_loss,
                                                           args.norm, args.targeted, args.target_type,
                                                           args.distillation_loss))
    os.makedirs(args.exp_dir, exist_ok=True)
    set_log_file(osp.join(args.exp_dir, 'run.log'))




    torch.backends.cudnn.deterministic = True
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.test_archs:
        archs = []
        if args.dataset == "CIFAR-10" or args.dataset == "CIFAR-100":
            for arch in MODELS_TEST_STANDARD[args.dataset]:
                test_model_path = "{}/train_pytorch_model/real_image_model/{}-pretrained/{}/checkpoint.pth.tar".format(PY_ROOT,
                                                                                        args.dataset,  arch)
                if os.path.exists(test_model_path):
                    archs.append(arch)
                else:
                    log.info(test_model_path + " does not exists!")
        else:
            for arch in MODELS_TEST_STANDARD[args.dataset]:
                test_model_list_path = "{}/train_pytorch_model/real_image_model/{}-pretrained/checkpoints/{}*.pth".format(
                    PY_ROOT,
                    args.dataset, arch)
                test_model_list_path = list(glob.glob(test_model_list_path))
                if len(test_model_list_path) == 0:  # this arch does not exists in args.dataset
                    continue
                archs.append(arch)
    else:
        assert args.arch is not None
        archs = [args.arch]
    args.arch = ", ".join(archs)
    log.info('Command line is: {}'.format(' '.join(sys.argv)))
    log.info("Log file is written in {}".format(osp.join(args.exp_dir, 'run.log')))
    log.info('Called with args:')
    print_args(args)
    attacker = SimulateBanditsAttackShrink(args, meta_finetuner)
    for arch in archs:
        save_result_path = args.exp_dir + "/{}_result.json".format(arch)
        if os.path.exists(save_result_path):
            continue
        log.info("Begin attack {} on {}, result will be saved to {}".format(arch, args.dataset, save_result_path))
        model = StandardModel(args.dataset, arch, no_grad=True)
        model.cuda()
        model.eval()
        attacker.attack_all_images(args, arch, model, save_result_path)
        model.cpu()
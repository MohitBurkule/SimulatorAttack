import glob
import os
import pickle
import random
import re
import numpy as np
import torch
from torch.utils import data
from config import IMAGE_SIZE, IN_CHANNELS, PY_ROOT, ALL_MODELS, MODELS_TRAIN, MODELS_TEST, CLASS_NUM
from constant_enum import SPLIT_DATA_PROTOCOL, LOAD_TASK_MODE


class TwoQueriesMetaTaskDataset(data.Dataset):
    def __init__(self, dataset, adv_norm, data_loss_type, tot_num_tasks, load_mode, protocol, targeted, target_type="random"):
        """
        Args:
            num_samples_per_class: num samples to generate "per class" in one batch
            batch_size: size of meta batch size (e.g. number of functions)
        """
        self.dataset = dataset
        if protocol == SPLIT_DATA_PROTOCOL.TRAIN_I_TEST_II:
            self.model_names = MODELS_TRAIN
        elif protocol == SPLIT_DATA_PROTOCOL.TRAIN_II_TEST_I:
            self.model_names = MODELS_TEST
        elif protocol == SPLIT_DATA_PROTOCOL.TRAIN_ALL_TEST_ALL:
            self.model_names = ALL_MODELS
        self.data_root_dir = "{}/data_bandit_attack/{}/{}".format(PY_ROOT, dataset, "targeted_attack" if targeted else "untargeted_attack")
        self.pattern = re.compile(".*arch_(.*?)@.*")
        self.train_files = []
        self.targeted = targeted
        self.tot_num_trn_tasks = tot_num_tasks
        for img_file_path in glob.glob(self.data_root_dir + "/dataset_{dataset}@attack_{norm}*loss_{loss_type}@{target_str}@images.npy".format(
                dataset=dataset, norm=adv_norm, loss_type=data_loss_type, target_str="targeted_" + target_type if targeted else "untargeted")):
            file_name = os.path.basename(img_file_path)
            ma = self.pattern.match(file_name)
            model_name = ma.group(1)
            if model_name in self.model_names:
                q1_path = img_file_path.replace("images.npy","q1.npy")
                q2_path = img_file_path.replace("images.npy", "q2.npy")
                logits_q1_path = img_file_path.replace("images.npy","logits_q1.npy")
                logits_q2_path = img_file_path.replace("images.npy","logits_q2.npy")
                shape_path = img_file_path.replace("images.npy", "shape.txt")
                gt_labels_path = img_file_path.replace("images.npy","gt_labels.npy")
                with open(shape_path, "r") as file_obj:
                    shape = eval(file_obj.read().strip())
                count = shape[0]
                seq_len = shape[1]
                gt_labels = np.load(gt_labels_path)
                each_file_json = {"count": count, "seq_len":seq_len, "image_path":img_file_path, "q1_path":q1_path,
                                  "q2_path":q2_path, "logits_q1_path":logits_q1_path,  "logits_q2_path":logits_q2_path,
                                  "gt_labels":gt_labels, "arch":model_name}
                if self.targeted:
                    targets_path = img_file_path.replace("images.npy","targets.npy")
                    each_file_json["targets"] = np.load(targets_path)
                self.train_files.append(each_file_json)
        self.data_attack_type = adv_norm
        target_str = "untargeted" if not targeted else "target_{}".format(target_type)
        self.task_dump_txt_path = "{}/task/{}_{}/{}_data_loss_type_{}_norm_{}_{}_tot_num_tasks_{}.pkl".format(PY_ROOT, protocol,
                                               dataset, dataset, data_loss_type, adv_norm, target_str, tot_num_tasks)
        self.store_tasks(load_mode, self.task_dump_txt_path, self.train_files)

    def store_tasks(self, load_mode, task_dump_txt_path, train_files):
        self.all_tasks = {}
        if load_mode == LOAD_TASK_MODE.LOAD and os.path.exists(task_dump_txt_path):
            with open(task_dump_txt_path, "rb") as file_obj:
                self.all_tasks = pickle.load(file_obj)
            return

        for i in range(self.tot_num_trn_tasks):  # 总共的训练任务个数，每次迭代都从这些任务去取
            if i % 1000 == 0:
                print("store {} tasks".format(i))
            file_entry = random.choice(train_files)
            entry = self.get_one_entry(file_entry)
            entry["task_idx"] = i
            self.all_tasks[i] = entry
        self.dump_task(self.all_tasks, task_dump_txt_path)

    # not used any more
    def get_gt_labels(self,gt_labels_path, all_count):
        fobj = open(gt_labels_path, "rb")
        all_gt_labels = []
        for seq_index in range(all_count):
            gt_label = np.memmap(fobj, dtype='float32', mode='r', shape=(1,),
                           offset=seq_index * 32 // 8).copy().reshape(1)[0]
            all_gt_labels.append(gt_label)
        fobj.close()
        return all_gt_labels

    def dump_task(self, all_tasks, task_dump_txt_path):
        os.makedirs(os.path.dirname(task_dump_txt_path),exist_ok=True)
        with open(task_dump_txt_path, "wb") as file_obj:
            pickle.dump(all_tasks, file_obj, protocol=True)

    def get_one_entry(self, file_entry):
        dict_with_img_index = {}
        dict_with_img_index.update(file_entry)
        count = file_entry["count"]
        seq_idx = random.randint(0, count-1)
        dict_with_img_index["index"] = seq_idx
        dict_with_img_index["gt_label"] = file_entry["gt_labels"][seq_idx]
        if self.targeted:
            dict_with_img_index["target"] = file_entry["targets"][seq_idx]
        return dict_with_img_index

    def __len__(self):
        return self.tot_num_trn_tasks

    def __getitem__(self, task_index):
        task_data = self.all_tasks[task_index]
        gt_label = task_data["gt_label"]
        if self.targeted:
            target = task_data["target"]
        seq_len = task_data["seq_len"]
        index = task_data["index"]
        with open(task_data["image_path"], "rb") as file_obj:
            adv_images = np.memmap(file_obj, dtype='float32', mode='r', shape=(seq_len, IN_CHANNELS[self.dataset],
                                                                               IMAGE_SIZE[self.dataset][0],
                                                                               IMAGE_SIZE[self.dataset][1]),
                                   offset=index * seq_len * IN_CHANNELS[self.dataset] * IMAGE_SIZE[self.dataset][0] * \
                                          IMAGE_SIZE[self.dataset][1] * 32 // 8).copy()
            adv_images = adv_images.reshape(seq_len, IN_CHANNELS[self.dataset], IMAGE_SIZE[self.dataset][0],
                                   IMAGE_SIZE[self.dataset][1])
        with open(task_data["q1_path"], "rb") as file_obj:
            q1 = np.memmap(file_obj, dtype='float32', mode='r', shape=(seq_len, IN_CHANNELS[self.dataset],
                                                                               IMAGE_SIZE[self.dataset][0],
                                                                               IMAGE_SIZE[self.dataset][1]),
                                   offset=index * seq_len * IN_CHANNELS[self.dataset] * IMAGE_SIZE[self.dataset][0] * \
                                          IMAGE_SIZE[self.dataset][1] * 32 // 8).copy()
            q1 = q1.reshape(seq_len, IN_CHANNELS[self.dataset], IMAGE_SIZE[self.dataset][0],
                                   IMAGE_SIZE[self.dataset][1])
        with open(task_data["q2_path"], "rb") as file_obj:
            q2 = np.memmap(file_obj, dtype='float32', mode='r', shape=(seq_len, IN_CHANNELS[self.dataset],
                                                                              IMAGE_SIZE[self.dataset][0],
                                                                              IMAGE_SIZE[self.dataset][1]),
                                  offset=index * seq_len * IN_CHANNELS[self.dataset] * IMAGE_SIZE[self.dataset][0] * \
                                         IMAGE_SIZE[self.dataset][1] * 32 // 8).copy()
            q2 = q2.reshape(seq_len, IN_CHANNELS[self.dataset], IMAGE_SIZE[self.dataset][0],
                                          IMAGE_SIZE[self.dataset][1])
        with open(task_data["logits_q1_path"], "rb") as file_obj:
            q1_logits = np.memmap(file_obj, dtype='float32', mode='r', shape=(seq_len, CLASS_NUM[self.dataset]),
                                  offset=index * seq_len * CLASS_NUM[self.dataset] * 32 // 8).copy()
        with open(task_data["logits_q2_path"], "rb") as file_obj:
            q2_logits = np.memmap(file_obj, dtype='float32', mode='r', shape=(seq_len, CLASS_NUM[self.dataset]),
                                  offset=index * seq_len * CLASS_NUM[self.dataset] * 32 // 8).copy()
        q1_images = adv_images + q1
        q2_images = adv_images + q2

        q1_images = torch.from_numpy(q1_images)
        q2_images = torch.from_numpy(q2_images)
        q1_logits = torch.from_numpy(q1_logits)
        q2_logits = torch.from_numpy(q2_logits)
        if self.targeted:
            return q1_images, q2_images, q1_logits, q2_logits, gt_label, target
        return q1_images, q2_images, q1_logits, q2_logits, gt_label



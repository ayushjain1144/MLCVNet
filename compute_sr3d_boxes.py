# Copyright (c) Facebook, Inc. and its affiliates.
# 
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

""" Evaluation routine for 3D object detection with SUN RGB-D and ScanNet.
"""

import os
import sys
import numpy as np
from datetime import datetime
import argparse
import importlib
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = BASE_DIR
sys.path.append(os.path.join(ROOT_DIR, 'models'))
from ap_helper import APCalculator, parse_predictions, parse_groundtruths
import ipdb
st = ipdb.set_trace

parser = argparse.ArgumentParser()
parser.add_argument('--dataset', default='scannet', help='Dataset name. sunrgbd or scannet. [default: scannet]')
parser.add_argument('--checkpoint_path', default=None, help='Model checkpoint path [default: None]')
parser.add_argument('--dump_dir', default=None, help='Dump dir to save sample outputs [default: None]')
parser.add_argument('--num_point', type=int, default=40000, help='Point Number [default: 40000]')
parser.add_argument('--num_target', type=int, default=256, help='Point Number [default: 256]')
parser.add_argument('--batch_size', type=int, default=8, help='Batch Size during training [default: 8]')
parser.add_argument('--vote_factor', type=int, default=1, help='Number of votes generated from each seed [default: 1]')
parser.add_argument('--cluster_sampling', default='seed_fps', help='Sampling strategy for vote clusters: vote_fps, seed_fps, random [default: seed_fps]')
parser.add_argument('--ap_iou_thresholds', default='0.25,0.5', help='A list of AP IoU thresholds [default: 0.25,0.5]')
parser.add_argument('--no_height', action='store_true', help='Do NOT use height signal in input.')
parser.add_argument('--use_color', action='store_true', help='Use RGB color in input.')
parser.add_argument('--use_sunrgbd_v2', action='store_true', help='Use SUN RGB-D V2 box labels.')
parser.add_argument('--use_3d_nms', action='store_true', help='Use 3D NMS instead of 2D NMS.')
parser.add_argument('--use_cls_nms', action='store_true', help='Use per class NMS.')
parser.add_argument('--use_old_type_nms', action='store_true', help='Use old type of NMS, IoBox2Area.')
parser.add_argument('--per_class_proposal', action='store_true', help='Duplicate each proposal num_class times.')
parser.add_argument('--nms_iou', type=float, default=0.25, help='NMS IoU threshold. [default: 0.25]')
parser.add_argument('--conf_thresh', type=float, default=0.05, help='Filter out predictions with obj prob less than it. [default: 0.05]')
parser.add_argument('--faster_eval', action='store_true', help='Faster evaluation by skippling empty bounding box removal.')
parser.add_argument('--shuffle_dataset', action='store_true', help='Shuffle the dataset (random order).')
FLAGS = parser.parse_args()

if FLAGS.use_cls_nms:
    assert(FLAGS.use_3d_nms)

# ------------------------------------------------------------------------- GLOBAL CONFIG BEG
BATCH_SIZE = FLAGS.batch_size
NUM_POINT = FLAGS.num_point
DUMP_DIR = FLAGS.dump_dir
CHECKPOINT_PATH = FLAGS.checkpoint_path
assert(CHECKPOINT_PATH is not None)
FLAGS.DUMP_DIR = DUMP_DIR
AP_IOU_THRESHOLDS = [float(x) for x in FLAGS.ap_iou_thresholds.split(',')]

# Prepare DUMP_DIR
if not os.path.exists(DUMP_DIR): os.mkdir(DUMP_DIR)
DUMP_FOUT = open(os.path.join(DUMP_DIR, 'log_eval.txt'), 'w')
DUMP_FOUT.write(str(FLAGS)+'\n')
def log_string(out_str):
    DUMP_FOUT.write(out_str+'\n')
    DUMP_FOUT.flush()
    print(out_str)

# Init datasets and dataloaders 
def my_worker_init_fn(worker_id):
    np.random.seed(np.random.get_state()[1][0] + worker_id)

if FLAGS.dataset == 'sunrgbd':
    sys.path.append(os.path.join(ROOT_DIR, 'sunrgbd'))
    from sunrgbd_detection_dataset import SunrgbdDetectionVotesDataset, MAX_NUM_OBJ
    from model_util_sunrgbd import SunrgbdDatasetConfig
    DATASET_CONFIG = SunrgbdDatasetConfig()
    TEST_DATASET = SunrgbdDetectionVotesDataset('val', num_points=NUM_POINT,
        augment=False, use_color=FLAGS.use_color, use_height=(not FLAGS.no_height),
        use_v1=(not FLAGS.use_sunrgbd_v2))
elif FLAGS.dataset == 'scannet':
    sys.path.append(os.path.join(ROOT_DIR, 'scannet'))
    from scannet_detection_dataset import ScannetDetectionDataset, MAX_NUM_OBJ
    from model_util_scannet import ScannetDatasetConfig
    DATASET_CONFIG = ScannetDatasetConfig()
    TEST_DATASET = ScannetDetectionDataset('val', num_points=NUM_POINT,
        augment=False,
        use_color=FLAGS.use_color, use_height=(not FLAGS.no_height))
else:
    print('Unknown dataset %s. Exiting...'%(FLAGS.dataset))
    exit(-1)
print(len(TEST_DATASET))
TEST_DATALOADER = DataLoader(TEST_DATASET, batch_size=BATCH_SIZE,
    shuffle=FLAGS.shuffle_dataset, num_workers=4, worker_init_fn=my_worker_init_fn)

# Init the model and optimzier
MODEL = importlib.import_module('mlcvnet') # import network module
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
num_input_channel = int(FLAGS.use_color)*3 + int(not FLAGS.no_height)*1

Detector = MODEL.MLCVNet

net = Detector(num_class=DATASET_CONFIG.num_class,
               num_heading_bin=DATASET_CONFIG.num_heading_bin,
               num_size_cluster=DATASET_CONFIG.num_size_cluster,
               mean_size_arr=DATASET_CONFIG.mean_size_arr,
               num_proposal=FLAGS.num_target,
               input_feature_dim=num_input_channel,
               vote_factor=FLAGS.vote_factor,
               sampling=FLAGS.cluster_sampling)
net.to(device)
criterion = MODEL.get_loss

# Load the Adam optimizer
optimizer = optim.Adam(net.parameters(), lr=0.001)

# Load checkpoint if there is any
if CHECKPOINT_PATH is not None and os.path.isfile(CHECKPOINT_PATH):
    checkpoint = torch.load(CHECKPOINT_PATH)
    net.load_state_dict(checkpoint['model_state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
    epoch = checkpoint['epoch']
    log_string("Loaded checkpoint %s (epoch: %d)"%(CHECKPOINT_PATH, epoch))

# Used for AP calculation
CONFIG_DICT = {'remove_empty_box': True, 'use_3d_nms': True, 'nms_iou': 0.25,
        'use_old_type_nms': False, 'cls_nms': False, 'per_class_proposal': False,
        'conf_thresh': 0.5, 'dataset_config': DATASET_CONFIG}
# ------------------------------------------------------------------------- GLOBAL CONFIG END

def evaluate_one_epoch():
    stat_dict = {}
    ap_calculator_list = [APCalculator(iou_thresh, DATASET_CONFIG.class2type) \
        for iou_thresh in AP_IOU_THRESHOLDS]
    net.eval() # set model to eval mode (for bn and dp)
    for batch_idx, batch_data_label in enumerate(TEST_DATALOADER):
        
        scan_name_list = batch_data_label['scan_name']
        del batch_data_label['scan_name']

        if batch_idx % 10 == 0:
            print('Eval batch: %d'%(batch_idx))

        for key in batch_data_label:
            batch_data_label[key] = batch_data_label[key].to(device)
        
        # Forward pass
        inputs = {'point_clouds': batch_data_label['point_clouds']}
        with torch.no_grad():
            end_points = net(inputs)



        # Compute loss
        for key in batch_data_label:
            assert(key not in end_points)
            end_points[key] = batch_data_label[key]
        loss, end_points = criterion(end_points, DATASET_CONFIG)

        # Accumulate statistics and print out
        for key in end_points:
            if 'loss' in key or 'acc' in key or 'ratio' in key:
                if key not in stat_dict: stat_dict[key] = 0
                stat_dict[key] += end_points[key].item()

        batch_pred_map_cls = parse_predictions(end_points, CONFIG_DICT) 
        batch_gt_map_cls = parse_groundtruths(end_points, CONFIG_DICT) 
        for ap_calculator in ap_calculator_list:
            ap_calculator.step(batch_pred_map_cls, batch_gt_map_cls)

        
        ######## Saving data ########
        save_dir = '/home/sirdome/katefgroup/language_grounding/mlcvnet_dump' 

        # INPUT
        point_clouds = end_points['point_clouds'].cpu().numpy()
        batch_size = point_clouds.shape[0]

        # NETWORK OUTPUTS
        seed_xyz = end_points['seed_xyz'].detach().cpu().numpy() # (B,num_seed,3)
        if 'vote_xyz' in end_points:
            aggregated_vote_xyz = end_points['aggregated_vote_xyz'].detach().cpu().numpy()
            vote_xyz = end_points['vote_xyz'].detach().cpu().numpy() # (B,num_seed,3)
            aggregated_vote_xyz = end_points['aggregated_vote_xyz'].detach().cpu().numpy()
        objectness_scores = end_points['objectness_scores'].detach().cpu().numpy() # (B,K,2)
        pred_center = end_points['center'].detach().cpu().numpy() # (B,K,3)
        pred_heading_class = torch.argmax(end_points['heading_scores'], -1) # B,num_proposal
        pred_heading_residual = torch.gather(end_points['heading_residuals'], 2, pred_heading_class.unsqueeze(-1)) # B,num_proposal,1
        pred_heading_class = pred_heading_class.detach().cpu().numpy() # B,num_proposal
        pred_heading_residual = pred_heading_residual.squeeze(2).detach().cpu().numpy() # B,num_proposal
        pred_size_class = torch.argmax(end_points['size_scores'], -1) # B,num_proposal
        pred_size_residual = torch.gather(end_points['size_residuals'], 2, pred_size_class.unsqueeze(-1).unsqueeze(-1).repeat(1,1,1,3)) # B,num_proposal,1,3
        pred_size_residual = pred_size_residual.squeeze(2).detach().cpu().numpy() # B,num_proposal,3

        # OTHERS
        pred_mask = end_points['pred_mask'] # B,num_proposal
        idx_beg = 0

        for i in range(batch_size):
            objectness_prob = softmax(objectness_scores[i,:,:])[:,1] # (K,)

            # Dump predicted bounding boxes
            if np.sum(objectness_prob>0.5)>0:
                num_proposal = pred_center.shape[1]
                sr3d_boxes = []
                for j in range(num_proposal):
                    sr3d_box = convert_mlcvnetbox_to_sr3d(DATASET_CONFIG, pred_center[i,j,0:3], 
                                                pred_size_class[i,j], pred_size_residual[i,j])
                    sr3d_boxes.append(sr3d_box)
                if len(sr3d_boxes)>0:
                    sr3d_boxes = np.vstack(tuple(sr3d_boxes)) # (num_proposal, 6)
                    # Output boxes according to their semantic labels
                    pred_sem_cls = torch.argmax(end_points['sem_cls_scores'], -1) # B,num_proposal
                    pred_sem_cls = pred_sem_cls.detach().cpu().numpy()
                    mask = np.logical_and(objectness_prob>0.5, pred_mask[i,:]==1)
                    sr3d_boxes = sr3d_boxes[mask, :]
                
                sr3d_boxes = list(sr3d_boxes)
            
            scan_name = scan_name_list[i]
            class_label_list = [DATASET_CONFIG.class2type[p[0]] for p in batch_pred_map_cls[i]]

            print(len(class_label_list))

            assert(len(sr3d_boxes) == len(class_label_list))

            data_dict = {
                "class": class_label_list,
                "box": sr3d_boxes
            }

            np.save(f'{save_dir}/{scan_name}.npy', data_dict)

        

    # Log statistics
    for key in sorted(stat_dict.keys()):
        log_string('eval mean %s: %f'%(key, stat_dict[key]/(float(batch_idx+1))))

    # Evaluate average precision
    for i, ap_calculator in enumerate(ap_calculator_list):
        print('-'*10, 'iou_thresh: %f'%(AP_IOU_THRESHOLDS[i]), '-'*10)
        metrics_dict = ap_calculator.compute_metrics()
        for key in metrics_dict:
            log_string('eval %s: %f'%(key, metrics_dict[key]))

    mean_loss = stat_dict['loss']/float(batch_idx+1)
    return mean_loss


def eval():
    log_string(str(datetime.now()))
    # Reset numpy seed.
    # REF: https://github.com/pytorch/pytorch/issues/5059
    np.random.seed()
    loss = evaluate_one_epoch()

def convert_mlcvnetbox_to_sr3d(DC, center, size_class, size_residual):
    box_size = DC.class2size(int(size_class), size_residual)

    lx, ly, lz = box_size
    xc, yc, zc = center

    xmin = xc - (lx / 2.0)
    ymin = yc - (ly / 2.0)
    zmin = zc - (lz / 2.0)
    xmax = xc + (lx / 2.0)
    ymax = yc + (ly / 2.0)
    zmax = zc + (lz / 2.0)

    return np.array([xmin, ymin, zmin, xmax, ymax, zmax])

def _convert_all_corners_to_end_points(box):
    """ converts 8X3 box to end points """
    xmin, ymin, zmin = np.min(box, axis=0)
    xmax, ymax, zmax = np.max(box, axis=0)

    return np.array([xmin, ymin, zmin, xmax, ymax, zmax]) 

def softmax(x):
    ''' Numpy function for softmax'''
    shape = x.shape
    probs = np.exp(x - np.max(x, axis=len(shape)-1, keepdims=True))
    probs /= np.sum(probs, axis=len(shape)-1, keepdims=True)
    return probs

if __name__=='__main__':
    eval()

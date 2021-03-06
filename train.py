#! /usr/bin/env python
# coding=utf-8
# ================================================================
#
#   Author      : miemie2013
#   Created date: 2020-05-20 15:35:27
#   Description : keras_yolov4
#
# ================================================================

import cv2
import math
import keras
import random
import numpy as np
import keras.layers as layers
from keras.callbacks import ModelCheckpoint, LambdaCallback
import os
import tensorflow as tf
from keras import backend as K
from model.yolov4 import YOLOv4

# 显存分配
os.environ["CUDA_VISIBLE_DEVICES"] = "0"
from keras.backend.tensorflow_backend import set_session
config = tf.ConfigProto()
config.gpu_options.per_process_gpu_memory_fraction = 1.0
set_session(tf.Session(config=config))

def bbox_ciou(boxes1, boxes2):
    '''
    计算ciou = iou - p2/c2 - av
    :param boxes1: (8, 13, 13, 3, 4)   pred_xywh
    :param boxes2: (8, 13, 13, 3, 4)   label_xywh
    :return:

    举例时假设pred_xywh和label_xywh的shape都是(1, 4)
    '''

    # 变成左上角坐标、右下角坐标
    boxes1_x0y0x1y1 = tf.concat([boxes1[..., :2] - boxes1[..., 2:] * 0.5,
                                 boxes1[..., :2] + boxes1[..., 2:] * 0.5], axis=-1)
    boxes2_x0y0x1y1 = tf.concat([boxes2[..., :2] - boxes2[..., 2:] * 0.5,
                                 boxes2[..., :2] + boxes2[..., 2:] * 0.5], axis=-1)
    '''
    逐个位置比较boxes1_x0y0x1y1[..., :2]和boxes1_x0y0x1y1[..., 2:]，即逐个位置比较[x0, y0]和[x1, y1]，小的留下。
    比如留下了[x0, y0]
    这一步是为了避免一开始w h 是负数，导致x0y0成了右下角坐标，x1y1成了左上角坐标。
    '''
    boxes1_x0y0x1y1 = tf.concat([tf.minimum(boxes1_x0y0x1y1[..., :2], boxes1_x0y0x1y1[..., 2:]),
                                 tf.maximum(boxes1_x0y0x1y1[..., :2], boxes1_x0y0x1y1[..., 2:])], axis=-1)
    boxes2_x0y0x1y1 = tf.concat([tf.minimum(boxes2_x0y0x1y1[..., :2], boxes2_x0y0x1y1[..., 2:]),
                                 tf.maximum(boxes2_x0y0x1y1[..., :2], boxes2_x0y0x1y1[..., 2:])], axis=-1)

    # 两个矩形的面积
    boxes1_area = (boxes1_x0y0x1y1[..., 2] - boxes1_x0y0x1y1[..., 0]) * (
                boxes1_x0y0x1y1[..., 3] - boxes1_x0y0x1y1[..., 1])
    boxes2_area = (boxes2_x0y0x1y1[..., 2] - boxes2_x0y0x1y1[..., 0]) * (
                boxes2_x0y0x1y1[..., 3] - boxes2_x0y0x1y1[..., 1])

    # 相交矩形的左上角坐标、右下角坐标，shape 都是 (8, 13, 13, 3, 2)
    left_up = tf.maximum(boxes1_x0y0x1y1[..., :2], boxes2_x0y0x1y1[..., :2])
    right_down = tf.minimum(boxes1_x0y0x1y1[..., 2:], boxes2_x0y0x1y1[..., 2:])

    # 相交矩形的面积inter_area。iou
    inter_section = tf.maximum(right_down - left_up, 0.0)
    inter_area = inter_section[..., 0] * inter_section[..., 1]
    union_area = boxes1_area + boxes2_area - inter_area
    iou = inter_area / (union_area + 1e-9)

    # 包围矩形的左上角坐标、右下角坐标，shape 都是 (8, 13, 13, 3, 2)
    enclose_left_up = tf.minimum(boxes1_x0y0x1y1[..., :2], boxes2_x0y0x1y1[..., :2])
    enclose_right_down = tf.maximum(boxes1_x0y0x1y1[..., 2:], boxes2_x0y0x1y1[..., 2:])

    # 包围矩形的对角线的平方
    enclose_wh = enclose_right_down - enclose_left_up
    enclose_c2 = K.pow(enclose_wh[..., 0], 2) + K.pow(enclose_wh[..., 1], 2)

    # 两矩形中心点距离的平方
    p2 = K.pow(boxes1[..., 0] - boxes2[..., 0], 2) + K.pow(boxes1[..., 1] - boxes2[..., 1], 2)

    # 增加av。加上除0保护防止nan。
    atan1 = tf.atan(boxes1[..., 2] / (boxes1[..., 3] + 1e-9))
    atan2 = tf.atan(boxes2[..., 2] / (boxes2[..., 3] + 1e-9))
    v = 4.0 * K.pow(atan1 - atan2, 2) / (math.pi ** 2)
    a = v / (1 - iou + v)

    ciou = iou - 1.0 * p2 / enclose_c2 - 1.0 * a * v
    return ciou


def bbox_iou(boxes1, boxes2):
    '''
    预测框          boxes1 (?, grid_h, grid_w, 3,   1, 4)，神经网络的输出(tx, ty, tw, th)经过了后处理求得的(bx, by, bw, bh)
    图片中所有的gt  boxes2 (?,      1,      1, 1, 150, 4)
    '''
    boxes1_area = boxes1[..., 2] * boxes1[..., 3]  # 所有格子的3个预测框的面积
    boxes2_area = boxes2[..., 2] * boxes2[..., 3]  # 所有ground truth的面积

    # (x, y, w, h)变成(x0, y0, x1, y1)
    boxes1 = tf.concat([boxes1[..., :2] - boxes1[..., 2:] * 0.5,
                        boxes1[..., :2] + boxes1[..., 2:] * 0.5], axis=-1)
    boxes2 = tf.concat([boxes2[..., :2] - boxes2[..., 2:] * 0.5,
                        boxes2[..., :2] + boxes2[..., 2:] * 0.5], axis=-1)

    # 所有格子的3个预测框 分别 和  150个ground truth  计算iou。 所以left_up和right_down的shape = (?, grid_h, grid_w, 3, 150, 2)
    left_up = tf.maximum(boxes1[..., :2], boxes2[..., :2])  # 相交矩形的左上角坐标
    right_down = tf.minimum(boxes1[..., 2:], boxes2[..., 2:])  # 相交矩形的右下角坐标

    inter_section = tf.maximum(right_down - left_up, 0.0)  # 相交矩形的w和h，是负数时取0     (?, grid_h, grid_w, 3, 150, 2)
    inter_area = inter_section[..., 0] * inter_section[..., 1]  # 相交矩形的面积            (?, grid_h, grid_w, 3, 150)
    union_area = boxes1_area + boxes2_area - inter_area  # union_area      (?, grid_h, grid_w, 3, 150)
    iou = 1.0 * inter_area / union_area  # iou                             (?, grid_h, grid_w, 3, 150)
    return iou

def loss_layer(conv, pred, label, bboxes, stride, num_class, iou_loss_thresh):
    conv_shape = tf.shape(conv)
    batch_size = conv_shape[0]
    output_size = conv_shape[1]
    input_size = stride * output_size
    conv = tf.reshape(conv, (batch_size, output_size, output_size,
                             3, 5 + num_class))
    conv_raw_prob = conv[:, :, :, :, 5:]

    pred_xywh = pred[:, :, :, :, 0:4]
    pred_conf = pred[:, :, :, :, 4:5]

    label_xywh = label[:, :, :, :, 0:4]
    respond_bbox = label[:, :, :, :, 4:5]
    label_prob = label[:, :, :, :, 5:]

    ciou = tf.expand_dims(bbox_ciou(pred_xywh, label_xywh), axis=-1)  # (8, 13, 13, 3, 1)
    input_size = tf.cast(input_size, tf.float32)

    # 每个预测框xxxiou_loss的权重 = 2 - (ground truth的面积/图片面积)
    bbox_loss_scale = 2.0 - 1.0 * label_xywh[:, :, :, :, 2:3] * label_xywh[:, :, :, :, 3:4] / (input_size ** 2)
    ciou_loss = respond_bbox * bbox_loss_scale * (1 - ciou)  # 1. respond_bbox作为mask，有物体才计算xxxiou_loss

    # 2. respond_bbox作为mask，有物体才计算类别loss
    prob_loss = respond_bbox * tf.nn.sigmoid_cross_entropy_with_logits(labels=label_prob, logits=conv_raw_prob)

    # 3. xxxiou_loss和类别loss比较简单。重要的是conf_loss，是一个focal_loss
    # 分两步：第一步是确定 grid_h * grid_w * 3 个预测框 哪些作为反例；第二步是计算focal_loss。
    expand_pred_xywh = pred_xywh[:, :, :, :, np.newaxis, :]  # 扩展为(?, grid_h, grid_w, 3,   1, 4)
    expand_bboxes = bboxes[:, np.newaxis, np.newaxis, np.newaxis, :, :]  # 扩展为(?,      1,      1, 1, 150, 4)
    iou = bbox_iou(expand_pred_xywh, expand_bboxes)  # 所有格子的3个预测框 分别 和  150个ground truth  计算iou。   (?, grid_h, grid_w, 3, 150)
    max_iou = tf.expand_dims(tf.reduce_max(iou, axis=-1), axis=-1)  # 与150个ground truth的iou中，保留最大那个iou。  (?, grid_h, grid_w, 3, 1)

    # respond_bgd代表  这个分支输出的 grid_h * grid_w * 3 个预测框是否是 反例（背景）
    # label有物体，respond_bgd是0。 没物体的话：如果和某个gt(共150个)的iou超过iou_loss_thresh，respond_bgd是0；如果和所有gt(最多150个)的iou都小于iou_loss_thresh，respond_bgd是1。
    # respond_bgd是0代表有物体，不是反例；  权重respond_bgd是1代表没有物体，是反例。
    # 有趣的是，模型训练时由于不断更新，对于同一张图片，两次预测的 grid_h * grid_w * 3 个预测框（对于这个分支输出）  是不同的。用的是这些预测框来与gt计算iou来确定哪些预测框是反例。
    # 而不是用固定大小（不固定位置）的先验框。
    respond_bgd = (1.0 - respond_bbox) * tf.cast(max_iou < iou_loss_thresh, tf.float32)

    # 二值交叉熵损失
    pos_loss = respond_bbox * (0 - K.log(pred_conf + 1e-9))
    neg_loss = respond_bgd  * (0 - K.log(1 - pred_conf + 1e-9))

    conf_loss = pos_loss + neg_loss
    # 回顾respond_bgd，某个预测框和某个gt的iou超过iou_loss_thresh，不被当作是反例。在参与“预测的置信位 和 真实置信位 的 二值交叉熵”时，这个框也可能不是正例(label里没标这个框是1的话)。这个框有可能不参与置信度loss的计算。
    # 这种框一般是gt框附近的框，或者是gt框所在格子的另外两个框。它既不是正例也不是反例不参与置信度loss的计算。（论文里称之为ignore）

    ciou_loss = tf.reduce_mean(tf.reduce_sum(ciou_loss, axis=[1, 2, 3, 4]))  # 每个样本单独计算自己的ciou_loss，再求平均值
    conf_loss = tf.reduce_mean(tf.reduce_sum(conf_loss, axis=[1, 2, 3, 4]))  # 每个样本单独计算自己的conf_loss，再求平均值
    prob_loss = tf.reduce_mean(tf.reduce_sum(prob_loss, axis=[1, 2, 3, 4]))  # 每个样本单独计算自己的prob_loss，再求平均值

    return ciou_loss + conf_loss + prob_loss
    # return ciou_loss

def decode(conv_output, anchors, stride, num_class):
    conv_shape       = tf.shape(conv_output)
    batch_size       = conv_shape[0]
    output_size      = conv_shape[1]
    anchor_per_scale = len(anchors)
    conv_output = tf.reshape(conv_output, (batch_size, output_size, output_size, anchor_per_scale, 5 + num_class))
    conv_raw_dxdy = conv_output[:, :, :, :, 0:2]
    conv_raw_dwdh = conv_output[:, :, :, :, 2:4]
    conv_raw_conf = conv_output[:, :, :, :, 4:5]
    conv_raw_prob = conv_output[:, :, :, :, 5: ]
    y = tf.tile(tf.range(output_size, dtype=tf.int32)[:, tf.newaxis], [1, output_size])
    x = tf.tile(tf.range(output_size, dtype=tf.int32)[tf.newaxis, :], [output_size, 1])
    xy_grid = tf.concat([x[:, :, tf.newaxis], y[:, :, tf.newaxis]], axis=-1)
    xy_grid = tf.tile(xy_grid[tf.newaxis, :, :, tf.newaxis, :], [batch_size, 1, 1, anchor_per_scale, 1])
    xy_grid = tf.cast(xy_grid, tf.float32)
    pred_xy = (tf.sigmoid(conv_raw_dxdy) + xy_grid) * stride
    pred_wh = (tf.exp(conv_raw_dwdh) * anchors) * stride
    pred_xywh = tf.concat([pred_xy, pred_wh], axis=-1)
    pred_conf = tf.sigmoid(conv_raw_conf)
    pred_prob = tf.sigmoid(conv_raw_prob)
    return tf.concat([pred_xywh, pred_conf, pred_prob], axis=-1)

def yolo_loss(args, num_classes, iou_loss_thresh, anchors):
    conv_lbbox = args[0]   # (?, ?, ?, 3*(num_classes+5))
    conv_mbbox = args[1]   # (?, ?, ?, 3*(num_classes+5))
    conv_sbbox = args[2]   # (?, ?, ?, 3*(num_classes+5))
    label_sbbox = args[3]   # (?, ?, ?, 3, num_classes+5)
    label_mbbox = args[4]   # (?, ?, ?, 3, num_classes+5)
    label_lbbox = args[5]   # (?, ?, ?, 3, num_classes+5)
    true_sbboxes = args[6]   # (?, 150, 4)
    true_mbboxes = args[7]   # (?, 150, 4)
    true_lbboxes = args[8]   # (?, 150, 4)
    pred_sbbox = decode(conv_sbbox, anchors[0], 8, num_classes)
    pred_mbbox = decode(conv_mbbox, anchors[1], 16, num_classes)
    pred_lbbox = decode(conv_lbbox, anchors[2], 32, num_classes)
    loss_sbbox = loss_layer(conv_sbbox, pred_sbbox, label_sbbox, true_sbboxes, 8, num_classes, iou_loss_thresh)
    loss_mbbox = loss_layer(conv_mbbox, pred_mbbox, label_mbbox, true_mbboxes, 16, num_classes, iou_loss_thresh)
    loss_lbbox = loss_layer(conv_lbbox, pred_lbbox, label_lbbox, true_lbboxes, 32, num_classes, iou_loss_thresh)
    return loss_sbbox + loss_mbbox + loss_lbbox

def get_classes(classes_path):
    with open(classes_path) as f:
        class_names = f.readlines()
    class_names = [c.strip() for c in class_names]
    return class_names

def training_transform(height, width, output_height, output_width):
    height_scale, width_scale = output_height / height, output_width / width
    scale = min(height_scale, width_scale)
    resize_height, resize_width = round(height * scale), round(width * scale)
    pad_top = (output_height - resize_height) // 2
    pad_left = (output_width - resize_width) // 2
    A = np.float32([[scale, 0.0], [0.0, scale]])
    B = np.float32([[pad_left], [pad_top]])
    M = np.hstack([A, B])
    return M, output_height, output_width

def image_preporcess(image, target_size, gt_boxes=None):
    # 这里改变了一部分原作者的代码。可以发现，传入训练的图片是bgr格式
    ih, iw = target_size
    h, w = image.shape[:2]
    M, h_out, w_out = training_transform(h, w, ih, iw)
    # 填充黑边缩放
    letterbox = cv2.warpAffine(image, M, (w_out, h_out))
    pimage = np.float32(letterbox) / 255.
    if gt_boxes is None:
        return pimage
    else:
        scale = min(iw / w, ih / h)
        nw, nh = int(scale * w), int(scale * h)
        dw, dh = (iw - nw) // 2, (ih - nh) // 2
        gt_boxes[:, [0, 2]] = gt_boxes[:, [0, 2]] * scale + dw
        gt_boxes[:, [1, 3]] = gt_boxes[:, [1, 3]] * scale + dh
        return pimage, gt_boxes

def random_fill(image, bboxes):
    if random.random() < 0.5:
        h, w, _ = image.shape
        # 水平方向填充黑边，以训练小目标检测
        if random.random() < 0.5:
            dx = random.randint(int(0.5*w), int(1.5*w))
            black_1 = np.zeros((h, dx, 3), dtype='uint8')
            black_2 = np.zeros((h, dx, 3), dtype='uint8')
            image = np.concatenate([black_1, image, black_2], axis=1)
            bboxes[:, [0, 2]] += dx
        # 垂直方向填充黑边，以训练小目标检测
        else:
            dy = random.randint(int(0.5*h), int(1.5*h))
            black_1 = np.zeros((dy, w, 3), dtype='uint8')
            black_2 = np.zeros((dy, w, 3), dtype='uint8')
            image = np.concatenate([black_1, image, black_2], axis=0)
            bboxes[:, [1, 3]] += dy
    return image, bboxes

def random_horizontal_flip(image, bboxes):
    if random.random() < 0.5:
        _, w, _ = image.shape
        image = image[:, ::-1, :]
        bboxes[:, [0,2]] = w - bboxes[:, [2,0]]
    return image, bboxes

def random_crop(image, bboxes):
    if random.random() < 0.5:
        h, w, _ = image.shape
        max_bbox = np.concatenate([np.min(bboxes[:, 0:2], axis=0), np.max(bboxes[:, 2:4], axis=0)], axis=-1)

        max_l_trans = max_bbox[0]
        max_u_trans = max_bbox[1]
        max_r_trans = w - max_bbox[2]
        max_d_trans = h - max_bbox[3]

        crop_xmin = max(0, int(max_bbox[0] - random.uniform(0, max_l_trans)))
        crop_ymin = max(0, int(max_bbox[1] - random.uniform(0, max_u_trans)))
        crop_xmax = max(w, int(max_bbox[2] + random.uniform(0, max_r_trans)))
        crop_ymax = max(h, int(max_bbox[3] + random.uniform(0, max_d_trans)))

        image = image[crop_ymin : crop_ymax, crop_xmin : crop_xmax]

        bboxes[:, [0, 2]] = bboxes[:, [0, 2]] - crop_xmin
        bboxes[:, [1, 3]] = bboxes[:, [1, 3]] - crop_ymin
    return image, bboxes

def random_translate(image, bboxes):
    if random.random() < 0.5:
        h, w, _ = image.shape
        max_bbox = np.concatenate([np.min(bboxes[:, 0:2], axis=0), np.max(bboxes[:, 2:4], axis=0)], axis=-1)

        max_l_trans = max_bbox[0]
        max_u_trans = max_bbox[1]
        max_r_trans = w - max_bbox[2]
        max_d_trans = h - max_bbox[3]

        tx = random.uniform(-(max_l_trans - 1), (max_r_trans - 1))
        ty = random.uniform(-(max_u_trans - 1), (max_d_trans - 1))

        M = np.array([[1, 0, tx], [0, 1, ty]])
        image = cv2.warpAffine(image, M, (w, h))

        bboxes[:, [0, 2]] = bboxes[:, [0, 2]] + tx
        bboxes[:, [1, 3]] = bboxes[:, [1, 3]] + ty
    return image, bboxes

def parse_annotation(annotation, train_input_size, annotation_type, pre_path):
    line = annotation.split()
    image_path = pre_path + line[0]
    if not os.path.exists(image_path):
        raise KeyError("%s does not exist ... " %image_path)
    image = np.array(cv2.imread(image_path))
    # 没有标注物品，即每个格子都当作背景处理
    exist_boxes = True
    if len(line) == 1:
        bboxes = np.array([[10, 10, 101, 103, 0]])
        exist_boxes = False
    else:
        bboxes = np.array([list(map(lambda x: int(float(x)), box.split(','))) for box in line[1:]])
    if annotation_type == 'train':
        # image, bboxes = random_fill(np.copy(image), np.copy(bboxes))    # 数据集缺乏小物体时打开
        image, bboxes = random_horizontal_flip(np.copy(image), np.copy(bboxes))
        image, bboxes = random_crop(np.copy(image), np.copy(bboxes))
        image, bboxes = random_translate(np.copy(image), np.copy(bboxes))
    image, bboxes = image_preporcess(np.copy(image), [train_input_size, train_input_size], np.copy(bboxes))
    return image, bboxes, exist_boxes

def bbox_iou_data(boxes1, boxes2):
    boxes1 = np.array(boxes1)
    boxes2 = np.array(boxes2)
    boxes1_area = boxes1[..., 2] * boxes1[..., 3]
    boxes2_area = boxes2[..., 2] * boxes2[..., 3]
    boxes1 = np.concatenate([boxes1[..., :2] - boxes1[..., 2:] * 0.5,
                            boxes1[..., :2] + boxes1[..., 2:] * 0.5], axis=-1)
    boxes2 = np.concatenate([boxes2[..., :2] - boxes2[..., 2:] * 0.5,
                            boxes2[..., :2] + boxes2[..., 2:] * 0.5], axis=-1)
    left_up = np.maximum(boxes1[..., :2], boxes2[..., :2])
    right_down = np.minimum(boxes1[..., 2:], boxes2[..., 2:])
    inter_section = np.maximum(right_down - left_up, 0.0)
    inter_area = inter_section[..., 0] * inter_section[..., 1]
    union_area = boxes1_area + boxes2_area - inter_area
    return inter_area / union_area

def preprocess_true_boxes(bboxes, train_output_sizes, strides, num_classes, max_bbox_per_scale, anchors):
    label = [np.zeros((train_output_sizes[i], train_output_sizes[i], 3,
                       5 + num_classes)) for i in range(3)]
    bboxes_xywh = [np.zeros((max_bbox_per_scale, 4)) for _ in range(3)]
    bbox_count = np.zeros((3,))
    for bbox in bboxes:
        bbox_coor = bbox[:4]
        bbox_class_ind = bbox[4]
        onehot = np.zeros(num_classes, dtype=np.float)
        onehot[bbox_class_ind] = 1.0
        bbox_xywh = np.concatenate([(bbox_coor[2:] + bbox_coor[:2]) * 0.5, bbox_coor[2:] - bbox_coor[:2]], axis=-1)
        bbox_xywh_scaled = 1.0 * bbox_xywh[np.newaxis, :] / strides[:, np.newaxis]
        iou = []
        for i in range(3):
            anchors_xywh = np.zeros((3, 4))
            anchors_xywh[:, 0:2] = np.floor(bbox_xywh_scaled[i, 0:2]).astype(np.int32) + 0.5
            anchors_xywh[:, 2:4] = anchors[i]
            iou_scale = bbox_iou_data(bbox_xywh_scaled[i][np.newaxis, :], anchors_xywh)
            iou.append(iou_scale)
        best_anchor_ind = np.argmax(np.array(iou).reshape(-1), axis=-1)
        best_detect = int(best_anchor_ind / 3)
        best_anchor = int(best_anchor_ind % 3)
        xind, yind = np.floor(bbox_xywh_scaled[best_detect, 0:2]).astype(np.int32)
        # 防止越界
        grid_r = label[best_detect].shape[0]
        grid_c = label[best_detect].shape[1]
        xind = max(0, xind)
        yind = max(0, yind)
        xind = min(xind, grid_r-1)
        yind = min(yind, grid_c-1)
        label[best_detect][yind, xind, best_anchor, :] = 0
        label[best_detect][yind, xind, best_anchor, 0:4] = bbox_xywh
        label[best_detect][yind, xind, best_anchor, 4:5] = 1.0
        label[best_detect][yind, xind, best_anchor, 5:] = onehot
        bbox_ind = int(bbox_count[best_detect] % max_bbox_per_scale)
        bboxes_xywh[best_detect][bbox_ind, :4] = bbox_xywh
        bbox_count[best_detect] += 1
    label_sbbox, label_mbbox, label_lbbox = label
    sbboxes, mbboxes, lbboxes = bboxes_xywh
    return label_sbbox, label_mbbox, label_lbbox, sbboxes, mbboxes, lbboxes

def generate_one_batch(annotation_lines, batch_size, anchors, num_classes, max_bbox_per_scale, pre_path, annotation_type):
    n = len(annotation_lines)
    i = 0
    while True:
        # 多尺度训练
        train_input_sizes = [320, 352, 384, 416, 448, 480, 512, 544, 576, 608]
        train_input_size = random.choice(train_input_sizes)
        strides = np.array([8, 16, 32])

        # 输出的网格数
        train_output_sizes = train_input_size // strides

        batch_image = np.zeros((batch_size, train_input_size, train_input_size, 3))

        batch_label_sbbox = np.zeros((batch_size, train_output_sizes[0], train_output_sizes[0],
                                      3, 5 + num_classes))
        batch_label_mbbox = np.zeros((batch_size, train_output_sizes[1], train_output_sizes[1],
                                      3, 5 + num_classes))
        batch_label_lbbox = np.zeros((batch_size, train_output_sizes[2], train_output_sizes[2],
                                      3, 5 + num_classes))

        batch_sbboxes = np.zeros((batch_size, max_bbox_per_scale, 4))
        batch_mbboxes = np.zeros((batch_size, max_bbox_per_scale, 4))
        batch_lbboxes = np.zeros((batch_size, max_bbox_per_scale, 4))

        for num in range(batch_size):
            if i == 0:
                np.random.shuffle(annotation_lines)

            image, bboxes, exist_boxes = parse_annotation(annotation_lines[i], train_input_size, annotation_type, pre_path)
            label_sbbox, label_mbbox, label_lbbox, sbboxes, mbboxes, lbboxes = preprocess_true_boxes(bboxes, train_output_sizes, strides, num_classes, max_bbox_per_scale, anchors)

            batch_image[num, :, :, :] = image
            if exist_boxes:
                batch_label_sbbox[num, :, :, :, :] = label_sbbox
                batch_label_mbbox[num, :, :, :, :] = label_mbbox
                batch_label_lbbox[num, :, :, :, :] = label_lbbox
                batch_sbboxes[num, :, :] = sbboxes
                batch_mbboxes[num, :, :] = mbboxes
                batch_lbboxes[num, :, :] = lbboxes
            i = (i + 1) % n
        yield [batch_image, batch_label_sbbox, batch_label_mbbox, batch_label_lbbox, batch_sbboxes, batch_mbboxes, batch_lbboxes], np.zeros(batch_size)

if __name__ == '__main__':
    # train_path = 'annotation/voc2012_train.txt'
    # val_path = 'annotation/voc2012_val.txt'
    # classes_path = 'data/voc_classes.txt'

    train_path = 'annotation/coco2017_train.txt'
    val_path = 'annotation/coco2017_val.txt'
    classes_path = 'data/coco_classes.txt'
    # 数据集图片的相对路径
    pre_path = '../COCO/'

    class_names = get_classes(classes_path)
    num_classes = len(class_names)
    anchors = np.array([
        [[12, 16], [19, 36], [40, 28]],
        [[36, 75], [76, 55], [72, 146]],
        [[142, 110], [192, 243], [459, 401]]
    ])
    # 一些预处理
    anchors = anchors.astype(np.float32)
    anchors[0] /= 8
    anchors[1] /= 16
    anchors[2] /= 32
    num_anchors = len(anchors[0])  # 每个输出层有几个先验框

    # 多尺度训练
    inputs = layers.Input(shape=(None, None, 3))
    model_body = YOLOv4(inputs, num_classes, num_anchors)

    # 模式。 0-从头训练，1-读取model_body继续训练（需要先运行脚本取得model_body）
    pattern = 1
    save_best_only = False
    max_bbox_per_scale = 150
    iou_loss_thresh = 0.7
    if pattern == 1:
        lr = 0.0001
        batch_size = 4
        initial_epoch = 0
        epochs = 49900
        model_path = 'yolov4.h5'
        model_body.load_weights(model_path, by_name=True)

        # 冻结，使得需要的显存减少。6G的卡建议这样配置。11G的卡建议不冻结以及批大小改为8。
        # freeze_before = 'conv2d_60'
        # freeze_before = 'conv2d_72'
        freeze_before = 'conv2d_86'
        for i in range(len(model_body.layers)):
            ly = model_body.layers[i]
            if ly.name == freeze_before:
                break
            else:
                ly.trainable = False
    elif pattern == 0:
        lr = 0.00001
        batch_size = 8
        initial_epoch = 0
        epochs = 20

    y_true = [
        layers.Input(name='input_2', shape=(None, None, 3, (num_classes + 5))),  # label_sbbox
        layers.Input(name='input_3', shape=(None, None, 3, (num_classes + 5))),  # label_mbbox
        layers.Input(name='input_4', shape=(None, None, 3, (num_classes + 5))),  # label_lbbox
        layers.Input(name='input_5', shape=(max_bbox_per_scale, 4)),             # true_sbboxes
        layers.Input(name='input_6', shape=(max_bbox_per_scale, 4)),             # true_mbboxes
        layers.Input(name='input_7', shape=(max_bbox_per_scale, 4))              # true_lbboxes
    ]
    model_loss = layers.Lambda(yolo_loss, output_shape=(1,), name='yolo_loss',
                           arguments={'num_classes': num_classes, 'iou_loss_thresh': iou_loss_thresh,
                                      'anchors': anchors})([*model_body.output, *y_true])
    model = keras.models.Model([model_body.input, *y_true], model_loss)
    model.summary()
    # keras.utils.vis_utils.plot_model(model_body, to_file='yolov4.png', show_shapes=True)

    # 回调函数
    checkpoint = ModelCheckpoint('./weights/ep{epoch:06d}-loss{loss:.3f}-val_loss{val_loss:.3f}.h5',
                                 monitor='val_loss', save_weights_only=False,
                                 save_best_only=save_best_only, period=1)
    # 回调函数，每轮训练结束后被调用，只保留最近10个模型文件
    def clear_models(epoch, logs):
        path_dir = os.listdir('./weights')
        eps = []
        names = []
        for name in path_dir:
            if name[len(name) - 2:len(name)] == 'h5' and name[0:2] == 'ep':
                sss = name.split('-')
                ep = int(sss[0][2:])
                eps.append(ep)
                names.append(name)
        if len(eps) > 10:
            i = eps.index(min(eps))
            os.remove(names[i])

    # 验证集和训练集
    with open(train_path) as f:
        train_lines = f.readlines()
    with open(val_path) as f:
        val_lines = f.readlines()
    num_train = len(train_lines)
    num_val = len(val_lines)

    # 保存模型的目录
    if not os.path.exists('./weights'): os.mkdir('./weights')

    model.compile(loss={'yolo_loss': lambda y_true, y_pred: y_pred}, optimizer=keras.optimizers.Adam(lr=lr))
    model.fit_generator(
        generator=generate_one_batch(train_lines, batch_size, anchors, num_classes, max_bbox_per_scale, pre_path, 'train'),
        steps_per_epoch=max(1, num_train // batch_size),
        validation_data=generate_one_batch(val_lines, batch_size, anchors, num_classes, max_bbox_per_scale, pre_path, 'val'),
        validation_steps=max(1, num_val // batch_size),
        epochs=epochs,
        initial_epoch=initial_epoch,
        callbacks=[checkpoint, LambdaCallback(on_epoch_end=clear_models)]
    )


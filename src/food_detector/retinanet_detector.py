#!/usr/bin/env python
from __future__ import absolute_import

import numpy as np
import rospy
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont
import torch

from pose_estimators.pose_estimator import PoseEstimator
from pose_estimators.detected_item import DetectedItem
from pose_estimators.utils.camera_subscriber import CameraSubscriber
from tf.transformations import quaternion_matrix, quaternion_from_euler

from .image_publisher import ImagePublisher
from .util import load_retinanet, load_label_map
from . import ada_feeding_demo_config as conf


class RetinaNetDetector(PoseEstimator, CameraSubscriber, ImagePublisher):
    def __init__(
            self,
            use_cuda,
            node_name=rospy.get_name(),
            retinanet_checkpoint=conf.checkpoint,
            label_map_file=conf.label_map,
            camera_tilt=1e-5,
            camera_to_table=conf.camera_to_table,
            frame=conf.camera_tf,
            image_topic='/camera/color/image_raw/compressed',
            image_msg_type='compressed',
            depth_image_topic='/camera/aligned_depth_to_color/image_raw',
            camera_info_topic='/camera/color/camera_info',
            detection_frame='camera_color_optical_frame',
            timeout=1.0,
            ema_alpha=1.0):

        PoseEstimator.__init__(self)
        CameraSubscriber.__init__(
            self,
            image_topic=image_topic,
            image_msg_type=image_msg_type,
            depth_image_topic=depth_image_topic,
            pointcloud_topic=None,
            camera_info_topic=camera_info_topic)
        ImagePublisher.__init__(self, node_name)

        print(retinanet_checkpoint)
        self.retinanet, self.retinanet_transform, self.encoder = \
            load_retinanet(use_cuda, retinanet_checkpoint)
        self.label_map = load_label_map(label_map_file)

        self.timeout = timeout

        self.selector_food_names = self.label_map.values()
        self.selector_index = 0

        self.use_cuda = use_cuda

        self.camera_tilt = camera_tilt
        self.camera_to_table = camera_to_table
        self.frame = frame
        self.ema_alpha = ema_alpha

        # Keeps track of previously detected items'
        # center of bounding boxes, class names and associate each
        # box x class with a unique id
        # Used fortracking (temporarily)
        self.detected_item_boxes = dict()
        # for food in self.selector_food_names:
            # self.detected_item_boxes[food] = dict()

        # This affects all items
        self.z = None

    def create_detected_item(self, rvec, tvec, t_class_name, box_id,
                             db_key='food_item', info_map=dict()):
        pose = quaternion_matrix(rvec)
        pose[:3, 3] = tvec

        return DetectedItem(
            frame_id=self.frame,
            marker_namespace=t_class_name,
            marker_id=box_id,
            db_key=db_key,
            pose=pose,
            detected_time=rospy.Time.now(),
            info_map=info_map)

    # Inherited classes change this
    def get_skewering_pose(self, txmin, txmax, tymin, tymax, width, height,
                           img_msg, t_class_name, annotation):
        """
        @return list of skewering position, angle,
        and other information for each detected item in the image.
        """
        return [[0.5, 0.5]], [0.0], [dict()]

    def find_closest_box_and_update(self, x, y, class_name,
        tolerance=70, angle=0.0, info=None):
        """
        Finds ths closest bounding box in the current list and
        updates it with the provided x, y
        @param x: Center x-position of a bounding box in 2D image
        @param y: Center y-position of a bounding box in 2D image
        @param class_name: Class name of the associated item
        @param tolerance: Pixel tolerance. If no box of same class is found
        within this tolerance, adds a new box with a new id
        @return Box id associated with the closest bounding box
        """
        min_distance = np.float('inf')
        matched_id = None
        largest_id = -1
        ids_to_delete = []

        if class_name not in self.detected_item_boxes:
            self.detected_item_boxes[class_name] = dict()

        for bid in self.detected_item_boxes[class_name].keys():
            item = self.detected_item_boxes[class_name][bid]
            bx, by = item[0], item[1]

            distance = np.linalg.norm(np.array([x, y]) - np.array([bx, by]))
            largest_id = max(largest_id, bid)
            if distance >= tolerance:
                continue
            if distance < min_distance:
                if matched_id:
                    # Pop this one, since we found a closer one
                    ids_to_delete.append(matched_id)
                min_distance = distance
                matched_id = bid
                # matched_position = (bx, by)

        if ids_to_delete:
            print("Delete ", ids_to_delete)
            for mid in ids_to_delete:
                self.detected_item_boxes[class_name].pop(mid)

        if matched_id is not None:
            # Perform EMA
            alpha = self.ema_alpha
            old_data = self.detected_item_boxes[class_name][matched_id]
            newx = alpha * x + (1.0 - alpha) * old_data[0]
            newy = alpha * y + (1.0 - alpha) * old_data[1]

            newa = alpha * angle + (1.0 - alpha) * old_data[2]
            print(old_data, newx, newy, newa, alpha)
            self.detected_item_boxes[class_name][matched_id] = (newx, newy, newa)
            
            if info is not None:
                if 'rotation' in info:
                    rotation = alpha * info['rotation'] + (1.0 - alpha) * old_data[3]
                    self.detected_item_boxes[class_name][matched_id] = (newx, newy, newa, rotation)
        else:
            self.detected_item_boxes[class_name][largest_id + 1] = (x, y, angle)
            if info is not None:
                if 'rotation' in info:
                    self.detected_item_boxes[class_name][largest_id + 1] = (x, y, angle, info['rotation'])
            matched_id = largest_id + 1
            print("Adding a new box with id {} for {}".format(
                matched_id, class_name))

        return matched_id

    def get_index_of_class_name(self, class_name):
        for index, name in self.label_map.items():
            if name == class_name:
                return index
        return -1

    def calculate_depth(self, depth_img):
        depth = depth_img.flatten()
        depth = depth[depth > 0]
        depth = depth[abs(depth - np.mean(depth)) < np.std(depth)]
        if depth is None or len(depth) == 0:
            return -1
        z0 = np.mean(depth)
        if self.z is None:
            self.z = z0
        else:
            z0 = self.ema_alpha * z0 + (1.0 - self.ema_alpha) * self.z
            self.z = z0
        return z0 / 1000.0  # mm to m

    # Entry point to initialize any annotation function with all boxes
    def annotation_initialization(self, boxes):
        pass

    # Entry point to annotate individual boxes with arbitrary data
    def annotate_box(self, box):
        return None

    def detect_objects(self):
        if self.img_msg is None:
            return list()

        if self.depth_img_msg is None:
            self.depth_img_msg = np.ones(self.img_msg.shape[:2])

        copied_img_msg = self.img_msg.copy()
        img = PILImage.fromarray(copied_img_msg.copy())
        depth_img = self.depth_img_msg.copy()

        width, height = img.size

        force_food = False
        force_food_name = "testberry"
        if (rospy.has_param('/deep_pose/forceFood') and
                rospy.has_param('/deep_pose/forceFoodName')):
            force_food = rospy.get_param('/deep_pose/forceFood')
            force_food_name = rospy.get_param('/deep_pose/forceFoodName')

        x = self.retinanet_transform(img)
        x = x.unsqueeze(0)
        with torch.no_grad():
            if self.use_cuda:
                loc_preds, cls_preds = self.retinanet(x.cuda())
            else:
                loc_preds, cls_preds = self.retinanet(x)

            boxes, labels, scores = self.encoder.decode(
                loc_preds.cpu().data.squeeze(),
                cls_preds.cpu().data.squeeze(),
                (width, height))

        if boxes is None or len(boxes) == 0:
            msg_img = self.bridge.cv2_to_imgmsg(np.array(img), "rgb8")
            self.pub_img.publish(msg_img)
            return list()

        # Intrinsic camera matrix for the raw (distorted) images.
        #     [fx  0 cx]
        # K = [ 0 fy cy]
        #     [ 0  0  1]
        # Projects 3D points in the camera coordinate frame to 2D pixel
        # coordinates using the focal lengths (fx, fy) and principal point
        # (cx, cy).
        camera_matrix = np.asarray(self.camera_info.K).reshape(3, 3)
        cam_fx = camera_matrix[0, 0]
        cam_fy = camera_matrix[1, 1]
        cam_cx = camera_matrix[0, 2]
        cam_cy = camera_matrix[1, 2]

        z0 = (self.camera_to_table /
              (np.cos(np.radians(90 - self.camera_tilt)) + 1e-10))
        # z0 = self.camera_to_table

        detections = list()

        bbox_offset = 5

        found = False
        spBoxIdx = -1
        for _ in range(len(self.selector_food_names)):
            for box_idx in range(len(boxes)):
                t_class = labels[box_idx].item()
                if t_class == -1:
                    t_class_name = 'sample'
                else:
                    t_class_name = self.label_map[t_class]

                if force_food:
                    t_class_name = force_food_name
                    t_class = self.get_index_of_class_name(t_class_name)

                #Overrides
                #if t_class_name == "grape":
                #    t_class_name = "strawberry"
                #if t_class_name == "cherry_tomato":
                #    t_class_name = "strawberry"
                if t_class_name == "honeydew":
                    t_class_name = "cantaloupe"

                if (t_class_name == list(self.selector_food_names)[self.selector_index]):
                    txmin, tymin, txmax, tymax = \
                        boxes[box_idx].numpy() - bbox_offset
                    if (txmin < 0 or tymin < 0 or
                            txmax > width or tymax > height):
                        continue
                    found = True
                    spBoxIdx = box_idx
                    self.last_class_name = t_class_name
                    break

            self.selector_index = ((self.selector_index + 1) %
                                   len(self.selector_food_names))
            if found:
                break

        chosen_boxes = []
        chosen_labels = []
        chosen_scores = []

        ### Begin Annotation Initialization
        # Label all food items to initialize any annotation algorithm
        boxes_labeled = []
        for box_idx in range(len(boxes)):
            box_labeled = {}

            # Get Location
            txmin, tymin, txmax, tymax = boxes[box_idx].numpy() - bbox_offset
            if (txmin < 0 or tymin < 0 or txmax > width or tymax > height):
                continue

            uv=[float(txmin + txmax) / 2.0, float(tymin + tymax) / 2.0]
            box_labeled['uv'] = uv

            # Get Class Name
            t_class = labels[box_idx].item()
            if t_class == -1:
                t_class_name = 'sample'
            else:
                t_class_name = self.label_map[t_class]
            if force_food:
                t_class_name = force_food_name

            #Overrides
            #if t_class_name == "grape":
            #    t_class_name = "strawberry"
            #if t_class_name == "cherry_tomato":
            #    t_class_name = "strawberry"
            if t_class_name == "honeydew":
                t_class_name = "cantaloupe"

            class_box_id = self.find_closest_box_and_update(
                        (txmin + txmax) / 2.0, (tymin + tymax) / 2.0,
                        t_class_name)

            box_labeled['id'] = class_box_id

            boxes_labeled.append(box_labeled)
        self.annotation_initialization(boxes_labeled)
        ### End Annotation Initialization

        for box_idx in range(len(boxes)):
            t_class = labels[box_idx].item()
            if t_class == -1:
                t_class_name = 'sample'
            else:
                t_class_name = self.label_map[t_class]
            if force_food:
                t_class_name = force_food_name
                t_class = self.get_index_of_class_name(t_class_name)

            #Overrides
            #if t_class_name == "grape":
            #    t_class_name = "strawberry"
            #if t_class_name == "cherry_tomato":
            #    t_class_name = "strawberry"
            if t_class_name == "honeydew":
                t_class_name = "cantaloupe"

          #  if t_class_name != 'strawberry':
          #      continue
            print(t_class_name)

            txmin, tymin, txmax, tymax = boxes[box_idx].numpy() - bbox_offset
            if (txmin < 0 or tymin < 0 or txmax > width or tymax > height):
                continue

            ### Begin Annotation
            box_labeled = {}
            box_labeled['id'] = self.find_closest_box_and_update(
                        (txmin + txmax) / 2.0, (tymin + tymax) / 2.0,
                        t_class_name)
            box_labeled['uv'] = [float(txmin + txmax) / 2.0, float(tymin + tymax) / 2.0]
            annotation = self.annotate_box(box_labeled)
            ### End Annotation

            skewer_xys, skewer_angles, skewer_infos = self.get_skewering_pose(
                    txmin, txmax, tymin, tymax, width, height,
                    copied_img_msg, t_class_name, annotation)

            if len(skewer_xys) == 0:
                self.visualize_detections(img, [], [], [], bbox_offset)
                continue

            for skewer_xy, skewer_angle, skewer_info in zip(skewer_xys, skewer_angles, skewer_infos):

                if "action" in skewer_info:
                    t_class_name_current = t_class_name + "+" + skewer_info["action"]
                else:
                    t_class_name_current = t_class_name

                txoff = (txmax - txmin) * skewer_xy[0]
                tyoff = (tymax - tymin) * skewer_xy[1]
                pt = [txmin + txoff, tymin + tyoff]

                class_box_id = self.find_closest_box_and_update(
                        pt[0], pt[1],
                        t_class_name_current,
                        angle=skewer_angle,
                        info=skewer_info)
                
                tup = self.detected_item_boxes[t_class_name_current][class_box_id]
                if len(tup) > 3:
                    pt[0], pt[1], this_ang, skewer_info['rotation'] = tup
                else:
                    pt[0], pt[1], this_ang = tup

                cropped_depth = depth_img[
                    int(max(tymin, 0)):int(min(tymax, height)),
                    int(max(txmin, 0)):int(min(txmax, width))]
                z0 = self.calculate_depth(cropped_depth)
                if z0 < 0:
                    # Skipping due to invalid z0
                    continue

                if spBoxIdx >= 0:
                    # this_pos = skewer_xy
                    this_ang = skewer_angle

                    # txoff = (txmax - txmin) * this_pos[0]
                    # tyoff = (tymax - tymin) * this_pos[1]
                    # pt = [txmin + txoff, tymin + tyoff]

                    coff = 60
                    cropped_depth = depth_img[
                        int(pt[1] - coff):int(pt[1] + coff),
                        int(pt[0] - coff):int(pt[1] + coff)]
                    current_z0 = self.calculate_depth(cropped_depth)
                    if (current_z0 < 0):
                        current_z0 = z0
                    print("current_z0", current_z0)
                    x, y, z, w = quaternion_from_euler(
                        0, 0., np.deg2rad(this_ang))
                    rvec = np.array([x, y, z, w])
                    print("Retinanet", rvec)

                    tz = current_z0
                    tx = (tz / cam_fx) * (pt[0] - cam_cx)
                    ty = (tz / cam_fy) * (pt[1] - cam_cy)
                    tvec = np.array([tx, ty, tz])

                    # NOTE: modified to t_class_name
                    detections.append(self.create_detected_item(
                        rvec, tvec, t_class_name, class_box_id, info_map=skewer_info))

                    chosen_boxes.append(boxes[box_idx])
                    chosen_labels.append(
                        "{}_{}".format(t_class_name, class_box_id))
                    chosen_scores.append(scores[box_idx])

        self.visualize_detections(
            img, chosen_boxes, chosen_scores, chosen_labels, bbox_offset)

        return detections

    def visualize_detections(self, img, boxes, scores, labels, bbox_offset):
        # visualize detections
        fnt = ImageFont.truetype('Pillow/Tests/fonts/DejaVuSans.ttf', 12)
        draw = ImageDraw.Draw(img, 'RGBA')

        for idx in range(len(boxes)):
            box = boxes[idx].numpy() - bbox_offset
            label = labels[idx]
            draw.rectangle(box, outline=(255, 0, 0, 200))
            box1 = box + 1
            box1[:2] -= 2
            draw.rectangle(box1, outline=(255, 0, 0, 200))
            box2 = box + 2
            box2[:2] -= 4
            draw.rectangle(box2, outline=(255, 0, 0, 200))

            item_tag = '{0}: {1:.2f}'.format(
                label,
                scores[idx])
            iw, ih = fnt.getsize(item_tag)
            ix, iy = box[:2]
            draw.rectangle((ix, iy, ix + iw, iy + ih), fill=(255, 0, 0, 100))
            draw.text(
                box[:2],
                item_tag,
                font=fnt, fill=(255, 255, 255, 255))

        msg_img = self.bridge.cv2_to_imgmsg(np.array(img), "rgb8")
        self.pub_img.publish(msg_img)

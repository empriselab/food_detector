import rospy
import os
import json
import argparse

from visualization_msgs.msg import Marker

from deep_pose_estimators.run_perception_module import run_detection
from deep_pose_estimators.perception_module import PerceptionModule
from deep_pose_estimators.marker_manager import MarkerManager

import ada_feeding_demo_config as conf
from food_detector import FoodDetector
from food_detector.retinanet_detector import RetinaNetDetector


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        "Run perception module for ada feeding projects")
    parser.add_argument(
        "--demo-type", choices=['spnet', 'action-score', 'retinanet'],
        required=True)
    args = parser.parse_args()

    if args.demo_type == 'retinanet':
        # TODO
        pose_estimator = RetinaNetDetector()
    elif args.demo_type == "spnet":
        import food_detector.ada_feeding_demo_config as conf
        # TODO: shall we allow other options?
        rospy.init_node(conf.node_name)
        pose_estimator = FoodDetector(use_cuda=True)
    else:
        scores = json.loads("score.json")  # does not exists yet
        action_types = ["vertical_skewer_0",
                        "vertical_tilted_skewer_0",
                        "vertical_angled_skewer_0"]
        pose_estimator = ActionDetector(
            scores, action_types, conf.checkpoint,
            use_cuda=conf.use_cuda,
            label_map_file=conf.label_map,
            publisher_topic=conf.node_title)

    if conf.use_cuda:
        os.environ['CUDA_VISIBLE_DEVICES'] = conf.gpus

    marker_manager = MarkerManager(
        marker_type=Marker.CUBE,
        scale=[0.01, 0.01, 0.01],
        color=[0.5, 1.0, 0.5, 0.1],
        count_items=False)  # spnet handles this internally

    perception_module = PerceptionModule(
        pose_estimator=pose_estimator,
        marker_manager=marker_manager,
        detection_frame_marker_topic=None,
        detection_frame=conf.camera_tf,
        destination_frame="map",
        purge_all_markers_per_update=True)

    print("run detection")
    run_detection(conf.node_name, conf.frequency, perception_module)

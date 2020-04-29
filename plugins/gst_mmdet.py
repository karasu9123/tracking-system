import numpy as np
import gi
import sys

gi.require_version('Gst', '1.0')
gi.require_version('GstBase', '1.0')
sys.path.append('../')
import common.is_aarch_64
import common.bus_call
from gi.repository import Gst, GObject, GstBase
from .gst_hacks import get_buffer_size, map_gst_buffer
from mmdet.apis import init_detector, inference_detector
import pyds

MMDET = 'mmdet'
UNTRACKED_OBJECT_ID = 0xFFFFFFFFFFFFFFFF
# Standard GStreamer initialization
GObject.threads_init()
Gst.init(None)


def register(plugin):
    type_to_register = GObject.type_register(MMDet)
    return Gst.Element.register(plugin, MMDET, 0, type_to_register)


def register_by_name(plugin_name):
    name = plugin_name
    description = "Performs network using MMDetection and writes detections to Deepstream metadata"
    version = '0.1.0'
    gst_license = 'LGPL'
    source_module = 'gstreamer'
    package = 'mmdet'
    origin = 'MLab'
    if not Gst.Plugin.register_static(Gst.VERSION_MAJOR, Gst.VERSION_MINOR,
                                      name, description,
                                      register, version, gst_license,
                                      source_module, package, origin):
        raise ImportError("Plugin {} not registered".format(plugin_name))
    return True


def non_max_suppression_fast(boxes, overlapThresh):
    # if there are no boxes, return an empty list
    if len(boxes) == 0:
        return []
    # if the bounding boxes integers, convert them to floats --
    # this is important since we'll be doing a bunch of divisions
    if boxes.dtype.kind == "i":
        boxes = boxes.astype("float")
    # initialize the list of picked indexes
    pick = []
    # grab the coordinates of the bounding boxes
    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
    # compute the area of the bounding boxes and sort the bounding
    # boxes by the bottom-right y-coordinate of the bounding box
    area = (x2 - x1 + 1) * (y2 - y1 + 1)
    idxs = np.argsort(y2)
    # keep looping while some indexes still remain in the indexes
    # list
    while len(idxs) > 0:
        # grab the last index in the indexes list and add the
        # index value to the list of picked indexes
        last = len(idxs) - 1
        i = idxs[last]
        pick.append(i)
        # find the largest (x, y) coordinates for the start of
        # the bounding box and the smallest (x, y) coordinates
        # for the end of the bounding box
        xx1 = np.maximum(x1[i], x1[idxs[:last]])
        yy1 = np.maximum(y1[i], y1[idxs[:last]])
        xx2 = np.minimum(x2[i], x2[idxs[:last]])
        yy2 = np.minimum(y2[i], y2[idxs[:last]])
        # compute the width and height of the bounding box
        w = np.maximum(0, xx2 - xx1 + 1)
        h = np.maximum(0, yy2 - yy1 + 1)
        # compute the ratio of overlap
        overlap = (w * h) / area[idxs[:last]]
        # delete all indexes from the index list that have
        idxs = np.delete(idxs, np.concatenate(([last],
                                               np.where(overlap > overlapThresh)[0])))
    # return only the bounding boxes that were picked using the
    # integer data type
    return boxes[pick]


class MMDet(GstBase.BaseTransform):
    CHANNELS = 4  # RGBA

    __gstmetadata__ = ("MMDet",
                       "BaseTransform",
                       "Perform detections",
                       "MLab")

    __gsttemplates__ = (Gst.PadTemplate.new("src",
                                            Gst.PadDirection.SRC,
                                            Gst.PadPresence.ALWAYS,
                                            Gst.Caps.from_string("video/x-raw,"
                                                                 "format=(string)RGBA,"
                                                                 "width=[1,2147483647],"
                                                                 "height=[1,2147483647],"
                                                                 "framerate=[0/1,2147483647/1]")),
                        Gst.PadTemplate.new("sink",
                                            Gst.PadDirection.SINK,
                                            Gst.PadPresence.ALWAYS,
                                            Gst.Caps.from_string("video/x-raw,"
                                                                 "format=(string)RGBA,"
                                                                 "width=[1,2147483647],"
                                                                 "height=[1,2147483647],"
                                                                 "framerate=[0/1,2147483647/1]")))

    __gproperties__ = {
        "config": (GObject.TYPE_PYOBJECT,
                   "config",
                   "A property that contains the path to a config",
                   GObject.ParamFlags.READWRITE
                   ),
        "checkpoint": (GObject.TYPE_PYOBJECT,
                       "checkpoint",
                       "A property that contains the path to a checkpoint",
                       GObject.ParamFlags.READWRITE
                       ),
        "threshold": (GObject.TYPE_PYOBJECT,
                      "threshold",
                      "A property that contains the confidence threshold",
                      GObject.ParamFlags.READWRITE
                      ),
        "nms": (GObject.TYPE_PYOBJECT,
                      "nms",
                      "A property that contains the nms threshold",
                      GObject.ParamFlags.READWRITE
                      )
    }

    def __init__(self):
        self.config = None
        self.checkpoint = None
        self.threshold = 0.5
        self.nms = 0.5
        self.model = None

        super(MMDet, self).__init__()

    def do_get_property(self, prop: GObject.GParamSpec):
        if prop.name == 'config':
            return self.config
        elif prop.name == 'checkpoint':
            return self.checkpoint
        elif prop.name == 'threshold':
            return self.threshold
        elif prop.name == 'nms':
            return self.nms
        else:
            raise AttributeError('unknown property %s' % prop.name)

    def do_set_property(self, prop: GObject.GParamSpec, value):
        if prop.name == 'config':
            self.config = value
            if self.checkpoint is not None and self.model is None:
                self.model = init_detector(self.config, self.checkpoint, device='cuda:0')
        elif prop.name == 'checkpoint':
            self.checkpoint = value
            if self.checkpoint is not None and self.model is None:
                self.model = init_detector(self.config, self.checkpoint, device='cuda:0')
        elif prop.name == 'threshold':
            self.threshold = value
        elif prop.name == 'nms':
            self.nms = value
        else:
            raise AttributeError('unknown property %s' % prop.name)

    def do_transform_ip(self, buf):
        success, (width, height) = get_buffer_size(self.srcpad.get_current_caps())
        if not success:
            return Gst.FlowReturn.ERROR

        with map_gst_buffer(buf, Gst.MapFlags.READ) as mapped:
            frame = np.ndarray((height, width, self.CHANNELS), buffer=mapped, dtype=np.uint8)

        result = inference_detector(self.model, frame[..., :3])

        if isinstance(result, tuple):
            bbox_result, segm_result = result
        else:
            bbox_result, segm_result = result, None

        # get class ids and detections as ndarrays
        class_ids = [
            np.full(bbox.shape[0], i, dtype=np.int32)
            for i, bbox in enumerate(bbox_result)
        ]
        class_ids = np.concatenate(class_ids)
        bboxes = np.vstack(bbox_result)

        # do thresholding by confidence
        scores = bboxes[:, -1]
        inds = scores > self.threshold
        bboxes = bboxes[inds, :]
        class_ids = class_ids[inds]

        # nms
        bboxes = non_max_suppression_fast(bboxes, self.nms)

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buf))
        l_frame = batch_meta.frame_meta_list
        while l_frame is not None:
            try:
                frame_meta = pyds.glist_get_nvds_frame_meta(l_frame.data)
            except StopIteration:
                break

            for class_id, bbox in zip(class_ids, bboxes):
                obj_meta = pyds.nvds_acquire_obj_meta_from_pool(batch_meta)
                obj_meta.class_id = class_id
                obj_meta.object_id = UNTRACKED_OBJECT_ID
                obj_meta.confidence = bbox[-1]
                obj_meta.rect_params.left = bbox[0]
                obj_meta.rect_params.top = bbox[1]
                obj_meta.rect_params.width = bbox[2] - bbox[0]
                obj_meta.rect_params.height = bbox[3] - bbox[1]
                obj_meta.rect_params.border_width = 2
                pyds.nvds_add_obj_meta_to_frame(frame_meta, obj_meta, None)
            try:
                l_frame = l_frame.next
            except StopIteration:
                break

        return Gst.FlowReturn.OK


register_by_name(MMDET)

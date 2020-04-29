import numpy as np
import cv2
import sys
import gi
from typing import NamedTuple

gi.require_version('Gst', '1.0')
gi.require_version('GstBase', '1.0')
from gi.repository import Gst, GObject, GstBase

sys.path.append('../')
import common.is_aarch_64
import common.bus_call
import pyds
from .gst_hacks import map_gst_buffer, get_buffer_size

META_DRAWER = 'metadrawer'
UNTRACKED_OBJECT_ID = 0xFFFFFFFFFFFFFFFF
DEFAULT_BBOX_COLORS = [
    (255.0, 75.0, 75.0),
    (75.0, 75.0, 255.0),
    (75.0, 255.0, 75.0)
]

# Standard GStreamer initialization
GObject.threads_init()
Gst.init(None)


class BBox(NamedTuple):
    left_top: tuple
    right_bottom: tuple
    color: tuple


class DisplayedObject(NamedTuple):
    bbox: BBox
    text: str


def draw_meta(frame, objects_meta):
    """ Draw object borders, ids, confidence """
    alpha = 0.15
    overlay = frame.copy()
    font_color = (255, 255, 255)
    font_bg_color = (0, 0, 0)
    font_scale = 0.5
    font_face = cv2.FONT_HERSHEY_SIMPLEX
    font_thickness = 1

    for obj_meta in objects_meta:
        # draw the transparent bbox
        cv2.rectangle(overlay, obj_meta.bbox.left_top, obj_meta.bbox.right_bottom, obj_meta.bbox.color, -1)
        cv2.rectangle(frame, obj_meta.bbox.left_top, obj_meta.bbox.right_bottom, obj_meta.bbox.color, 1)

        # draw text and the text backgrounds
        if obj_meta.text is not None:
            size, base_line = cv2.getTextSize(obj_meta.text, font_face, font_scale, font_thickness)
            bg_left_top = (obj_meta.bbox.left_top[0], obj_meta.bbox.left_top[1] - size[1])
            bg_right_bottom = (obj_meta.bbox.left_top[0] + size[0], obj_meta.bbox.left_top[1] + 3)
            cv2.rectangle(frame, bg_left_top, bg_right_bottom, font_bg_color, -1)
            cv2.putText(frame, obj_meta.text, obj_meta.bbox.left_top, font_face, font_scale, font_color, font_thickness)

    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)


def register(plugin):
    type_to_register = GObject.type_register(MetaDrawer)
    return Gst.Element.register(plugin, META_DRAWER, 0, type_to_register)


def register_by_name(plugin_name):
    name = plugin_name
    description = "Draws DeepStream meta on frames"
    version = '0.1.0'
    gst_license = 'LGPL'
    source_module = 'gstreamer'
    package = 'metadrawer'
    origin = 'mlab'
    if not Gst.Plugin.register_static(Gst.VERSION_MAJOR, Gst.VERSION_MINOR,
                                      name, description,
                                      register, version, gst_license,
                                      source_module, package, origin):
        raise ImportError("Plugin {} not registered".format(plugin_name))
    return True


class MetaDrawer(GstBase.BaseTransform):
    CHANNELS = 4  # RGBA

    __gstmetadata__ = ("MetaDrawer",
                       "BaseTransform",
                       "Draw bounding boxes, labels, ids and gates to the frame",
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
        "bbox-colors": (GObject.TYPE_PYOBJECT,
                        "bbox-colors",
                        "A property that contains the list of colors for bboxes",
                        GObject.ParamFlags.READWRITE
                        )
    }

    def __init__(self):
        self.bbox_colors = DEFAULT_BBOX_COLORS

        super(MetaDrawer, self).__init__()

    def do_get_property(self, prop: GObject.GParamSpec):
        if prop.name == 'bbox-colors':
            return self.bbox_colors
        else:
            raise AttributeError('unknown property %s' % prop.name)

    def do_set_property(self, prop: GObject.GParamSpec, value):
        if prop.name == 'bbox-colors':
            self.bbox_colors = value
        else:
            raise AttributeError('unknown property %s' % prop.name)

    def do_transform_ip(self, buf):
        success, (width, height) = get_buffer_size(self.srcpad.get_current_caps())
        if not success:
            return Gst.FlowReturn.ERROR

        with map_gst_buffer(buf, Gst.MapFlags.READ) as mapped:
            frame = np.ndarray((height, width, self.CHANNELS), buffer=mapped, dtype=np.uint8)

        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buf))
        l_frame = batch_meta.frame_meta_list
        while l_frame is not None:
            try:
                frame_meta = pyds.glist_get_nvds_frame_meta(l_frame.data)
            except StopIteration:
                break
            objects_meta = []
            l_obj = frame_meta.obj_meta_list
            while l_obj is not None:
                try:
                    obj_meta = pyds.glist_get_nvds_object_meta(l_obj.data)
                    left_top = (obj_meta.rect_params.left,
                                obj_meta.rect_params.top)
                    right_bottom = (obj_meta.rect_params.left + obj_meta.rect_params.width,
                                    obj_meta.rect_params.top + obj_meta.rect_params.height)
                    color = self.bbox_colors[obj_meta.class_id % len(self.bbox_colors)]
                    bbox = BBox(left_top, right_bottom, color)
                    text = None
                    if obj_meta.object_id != UNTRACKED_OBJECT_ID:
                        text = f"{obj_meta.object_id}"
                    objects_meta.append(DisplayedObject(bbox, text))
                    l_obj = l_obj.next
                except StopIteration:
                    break
            try:
                draw_meta(frame, objects_meta)
                l_frame = l_frame.next
            except StopIteration:
                break

        refcount = buf.mini_object.refcount
        buf.mini_object.refcount = 1

        with map_gst_buffer(buf, Gst.MapFlags.READ | Gst.MapFlags.WRITE) as mapped:
            out = np.ndarray((height, width, self.CHANNELS), buffer=mapped, dtype=np.uint8)
            out[:] = frame

        buf.mini_object.refcount += refcount - 1

        return Gst.FlowReturn.OK


register_by_name(META_DRAWER)
import numpy as np
import sys
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstBase', '1.0')
from gi.repository import Gst, GObject, GstBase
sys.path.append('../')
import common.is_aarch_64
import common.bus_call
import pyds
from .sort import Sort

GST_SORT = 'gstsort'
UNTRACKED_OBJECT_ID = 0xFFFFFFFFFFFFFFFF

# Standard GStreamer initialization
GObject.threads_init()
Gst.init(None)


def register(plugin):
    type_to_register = GObject.type_register(GstSORT)
    return Gst.Element.register(plugin, GST_SORT, 0, type_to_register)


def register_by_name(plugin_name):
    name = plugin_name
    description = "Track detections from Deepstream metadata"
    version = '0.1.0'
    gst_license = 'LGPL'
    source_module = 'gstreamer'
    package = 'gstsort'
    origin = 'MLab'
    if not Gst.Plugin.register_static(Gst.VERSION_MAJOR, Gst.VERSION_MINOR,
                                      name, description,
                                      register, version, gst_license,
                                      source_module, package, origin):
        raise ImportError("Plugin {} not registered".format(plugin_name))
    return True


class GstSORT(GstBase.BaseTransform):
    CHANNELS = 4  # RGBA

    __gstmetadata__ = ("GstSORT",
                       "BaseTransform",
                       "Tracker",
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

    def __init__(self):
        self.sort = Sort()
        self.tracks = []
        super(GstSORT, self).__init__()

    def do_transform_ip(self, buf):
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(hash(buf))
        l_frame = batch_meta.frame_meta_list

        while l_frame is not None:
            try:
                frame_meta = pyds.glist_get_nvds_frame_meta(l_frame.data)
            except StopIteration:
                break

            l_obj = frame_meta.obj_meta_list
            detected_objects = []
            objects_meta = []
            while l_obj is not None:
                try:
                    # Casting l_obj.data to pyds.NvDsObjectMeta
                    obj_meta = pyds.glist_get_nvds_object_meta(l_obj.data)
                    objects_meta.append(obj_meta)
                    detected_objects.append(
                        [obj_meta.rect_params.left, obj_meta.rect_params.top, obj_meta.rect_params.left + obj_meta.rect_params.width,
                         obj_meta.rect_params.top + obj_meta.rect_params.height, obj_meta.confidence, obj_meta.class_id])
                except StopIteration:
                    break
                try:
                    l_obj = l_obj.next
                except StopIteration:
                    break

            self.tracks = self.sort.update(np.array(detected_objects), 5)

            for object_index in range(len(self.tracks)):
                for detected_object_index in range(len(detected_objects)):
                    x = round(detected_objects[detected_object_index][0])
                    y = round(detected_objects[detected_object_index][1])
                    if (x - 2.5 < self.tracks[object_index][0] < x + 2.5) and (
                            y - 2.5 < self.tracks[object_index][1] < y + 2.5):
                        obj_id = int(self.tracks[object_index][4])
                        if obj_id is not None:
                            objects_meta[detected_object_index].object_id = obj_id
                        break
            try:
                l_frame = l_frame.next
            except StopIteration:
                break
        return Gst.FlowReturn.OK

register_by_name(GST_SORT)

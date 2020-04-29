import gi
import sys
import argparse

gi.require_version('Gst', '1.0')
from gi.repository import Gst, GObject, GLib
from common.bus_call import bus_call
from plugins import gst_mmdet
from plugins import gst_sort
from plugins import meta_drawer

# Standard GStreamer initialization
GObject.threads_init()
Gst.init(None)
net_input_size = (400, 400)


def uridecodebin_newpad(uridecodebin, uridecodebin_src_pad, sink_pad):
    pipeline, sink_pad = sink_pad
    print("\nIn uridecodebin_newpad ")
    new_pad_caps = uridecodebin_src_pad.get_current_caps()
    new_pad_struct = new_pad_caps.get_structure(0)
    new_pad_type = new_pad_struct.get_name()
    new_pad_features = new_pad_caps.get_features(0)
    print("\tnew_pad_type = ", new_pad_type)
    print("\tnew_pad_features = ", new_pad_features.to_string())

    if sink_pad.is_linked():
        print("\tWe are already linked. Ignoring. ")
        return

    if new_pad_type.find("video") != -1:
        print("\tThe pads are linking...")
        if new_pad_features.contains("memory:NVMM"):
            uridecodebin_src_pad.link(sink_pad)
        else:
            sys.stderr.write(" Error: Decodebin did not pick nvidia decoder plugin.\n")
        print("\tThe pads are successfully linked.")

    Gst.debug_bin_to_dot_file(pipeline, Gst.DebugGraphDetails.ALL, "pipeline")


def decoder_added(uridecodebin, sub_bin, element, batch_size):
    try:
        nvv4l2decoder_type = Gst.ElementFactory.find("nvv4l2decoder").get_element_type()
        element_type = element.get_factory().get_element_type()
        if element_type == nvv4l2decoder_type:
            element.set_property("num-extra-surfaces", batch_size - 4)
    except:
        pass


def enable_factory(name, enable=True):
    registry = Gst.Registry.get()
    if registry is None:
        return

    factory = Gst.ElementFactory.find(name)
    if factory is None:
        return

    if enable:
        factory.set_rank(0xFFFFFFF)
    else:
        factory.set_rank(Gst.Rank.NONE)
    registry.add_feature(factory)


def main():
    batch_size = 1
    second = 1000000000  # sec in nanosec

    ap = argparse.ArgumentParser("Prepare and run tracking system on Deepstream/MMDetection")
    ap.add_argument("-v", "--video", required=True, type=str, help="URL to video (e.g. file:///home/test.mp4)")
    ap.add_argument("-d", "--detector", required=True, choices=['nvinfer', 'mmdetection'], help="Detector type")
    ap.add_argument("-t", "--tracker", required=True, choices=['nvtracker', 'sort'], help="Tracker type")
    ap.add_argument("-c", "--confidence", default=0.5, type=float, help="Detection confidence threshold (0, 1)")
    ap.add_argument("-n", "--nms", default=0.3, type=float, help="Non maximum suppression threshold (0, 1)")
    ap.add_argument("--height", default=504, type=int, help="Frame height for processing")
    ap.add_argument("--width", default=504, type=int, help="Frame width for processing")
    ap.add_argument("--detector-config", required=True, type=str, help="Config file of detector")
    ap.add_argument("--detector-checkpoint", type=str, help="Checkpoint file of detector")
    ap.add_argument("--tracker-lib", type=str, help="Custom lib for nvtracker")
    ap.add_argument("--tracker-config", type=str, help="Config file of tracker")

    args = vars(ap.parse_args())

    enable_factory("nvv4l2decoder", True)
    enable_factory("nvjpegdec", True)

    # region Creating pipeline elements
    print("Creating Pipeline \n ")
    pipeline = Gst.Pipeline()
    if not pipeline:
        sys.stderr.write("\tUnable to create Pipeline \n")

    print("Creating uridecodebin \n")
    uridecodebin = Gst.ElementFactory.make("uridecodebin", "uridecodebin")
    if not uridecodebin:
        sys.stderr.write("\tUnable to create uridecodebin \n")

    print("Creating nvstreammux \n")
    nvstreammux = Gst.ElementFactory.make("nvstreammux", "nvstreammux")
    if not nvstreammux:
        sys.stderr.write("\tUnable to create nvstreammux \n")

    print("Creating nvvideoconvert0 \n")
    nvvideoconvert0 = Gst.ElementFactory.make("nvvideoconvert", "nvvideoconvert0")
    if not nvvideoconvert0:
        sys.stderr.write("\tUnable to create nvvideoconvert0 \n")

    print("Creating nvvideoconvert1 \n")
    nvvideoconvert1 = Gst.ElementFactory.make("nvvideoconvert", "nvvideoconvert1")
    if not nvvideoconvert1:
        sys.stderr.write("\tUnable to create nvvideoconvert1 \n")

    print("Creating nvvideoconvert2 \n")
    nvvideoconvert2 = Gst.ElementFactory.make("nvvideoconvert", "nvvideoconvert2")
    if not nvvideoconvert2:
        sys.stderr.write("\tUnable to create nvvideoconvert2 \n")

    print("Creating detector \n")
    if args["detector"] == "nvinfer":
        detector = Gst.ElementFactory.make("nvinfer", "detector")
        if not detector:
            sys.stderr.write("\tUnable to create nvinfer \n")
    elif args["detector"] == "mmdetection":
        detector = Gst.ElementFactory.make("mmdet", "detector")
        if not detector:
            sys.stderr.write("\tUnable to create mmdet \n")

    print("Creating tracker \n")
    if args["tracker"] == "nvtracker":
        tracker = Gst.ElementFactory.make("nvtracker", "tracker")
        if not tracker:
            sys.stderr.write("\tUnable to create nvtracker \n")
    elif args["tracker"] == "sort":
        tracker = Gst.ElementFactory.make("gstsort", "tracker")
        if not tracker:
            sys.stderr.write("\tUnable to create gstsort \n")

    print("Creating display_queue \n")
    display_queue = Gst.ElementFactory.make("queue", "display_queue")
    if not display_queue:
        sys.stderr.write("\tUnable to create display_queue \n")

    print("Creating metadrawer \n")
    metadrawer = Gst.ElementFactory.make("metadrawer", "metadrawer")
    if not metadrawer:
        sys.stderr.write("\tUnable to create metadrawer \n")

    print("Creating nveglglessink \n")
    nveglglessink = Gst.ElementFactory.make("nveglglessink", "nveglglessink")
    if not nveglglessink:
        sys.stderr.write("\tUnable to create nveglglessink \n")
    # endregion

    # region Setting properties
    print("Setting properties \n")
    uridecodebin.set_property('uri', args["video"])
    nvstreammux.set_property('width', args["width"])
    nvstreammux.set_property('height', args["height"])
    nvstreammux.set_property('buffer-pool-size', batch_size)
    nvstreammux.set_property('batch-size', batch_size)
    nvstreammux.set_property('batched-push-timeout', 1000000)
    if args["detector"] == "nvinfer":
        detector.set_property('config-file-path', args["detector_config"])
    elif args["detector"] == "mmdetection":
        detector.set_property('config', args["detector_config"])
        detector.set_property('checkpoint', args["detector_checkpoint"])
        detector.set_property('threshold', args["confidence"])
        detector.set_property('nms', args["nms"])
    if args["tracker"] == "nvtracker":
        tracker.set_property('tracker-width', args["width"])
        tracker.set_property('tracker-height', args["height"])
        tracker.set_property('ll-lib-file', args["tracker_lib"])
        tracker.set_property('ll-config-file', args["tracker_config"])
    nveglglessink.set_property('sync', False)
    # endregion

    print("Adding elements to the Pipeline \n")
    pipeline.add(uridecodebin)
    pipeline.add(nvstreammux)
    pipeline.add(nvvideoconvert0)
    pipeline.add(nvvideoconvert1)
    pipeline.add(nvvideoconvert2)
    pipeline.add(detector)
    pipeline.add(tracker)
    pipeline.add(metadrawer)
    pipeline.add(display_queue)
    pipeline.add(nveglglessink)
    # endregion

    # region Linking the elements
    print("Linking the elements \n")
    mux_sink = nvstreammux.get_request_pad("sink_0")
    uridecodebin.connect("pad-added", uridecodebin_newpad, (pipeline, mux_sink))
    uridecodebin.connect("deep-element-added", decoder_added, batch_size)
    nvstreammux.link(nvvideoconvert0)
    nvvideoconvert0.link(detector)
    detector.link(nvvideoconvert1)
    nvvideoconvert1.link(tracker)
    tracker.link(nvvideoconvert2)
    nvvideoconvert2.link(metadrawer)
    metadrawer.link(display_queue)
    display_queue.link(nveglglessink)
    # endregion

    # create an event loop and feed gstreamer bus mesages to it
    loop = GLib.MainLoop()
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", bus_call, loop)

    # start play back and listen to events
    print("Starting pipeline \n")
    print("Playing file %s \n" % args["video"])
    pipeline.set_state(Gst.State.PLAYING)
    try:
        loop.run()
    except:
        pass

    print("Cleaning pipeline \n")
    Gst.debug_bin_to_dot_file(pipeline, Gst.DebugGraphDetails.ALL, "pipeline")
    pipeline.set_state(Gst.State.NULL)


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""Menu bar app for Audio Router."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

from AppKit import (
    NSApplication,
    NSBezelBorder,
    NSColor,
    NSFont,
    NSImage,
    NSMakeRect,
    NSPopover,
    NSPopoverAppearanceHUD,
    NSPopoverBehaviorTransient,
    NSStatusBar,
    NSVariableStatusItemLength,
    NSView,
    NSApplicationActivationPolicyAccessory,
    NSApplicationDidFinishLaunchingNotification,
    NSControlSizeSmall,
    NSPopUpButton,
    NSSlider,
    NSButton,
    NSBox,
    NSTextField,
    NSMakeSize,
)
from Foundation import NSObject, NSNotificationCenter

API_BASE = "http://127.0.0.1:8080"


def api_get(path):
    try:
        url = f"{API_BASE}{path}"
        with urllib.request.urlopen(url, timeout=2) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def api_post(path, data=None):
    try:
        url = f"{API_BASE}{path}"
        body = json.dumps(data or {}).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def make_label(text, frame, font_size=11, color=None):
    label = NSTextField.alloc().initWithFrame_(frame)
    label.setStringValue_(text)
    label.setEditable_(False)
    label.setBordered_(False)
    label.setDrawsBackground_(False)
    label.setFont_(NSFont.systemFontOfSize_(font_size))
    if color:
        label.setTextColor_(color)
    return label


def make_slider(frame, min_val, max_val, value, target, action):
    slider = NSSlider.alloc().initWithFrame_(frame)
    slider.setMinValue_(min_val)
    slider.setMaxValue_(max_val)
    slider.setFloatValue_(value)
    slider.setTarget_(target)
    slider.setAction_(action)
    slider.setControlSize_(NSControlSizeSmall)
    return slider


def make_button(frame, title, target, action):
    btn = NSButton.alloc().initWithFrame_(frame)
    btn.setTitle_(title)
    btn.setBezelStyle_(6)
    btn.setTarget_(target)
    btn.setAction_(action)
    btn.setControlSize_(NSControlSizeSmall)
    return btn


def make_popup_button(frame, items, target, action):
    popup = NSPopUpButton.alloc().initWithFrame_(frame, pullsDown=False)
    popup.addItemsWithTitles_(items)
    popup.setTarget_(target)
    popup.setAction_(action)
    popup.setControlSize_(NSControlSizeSmall)
    return popup


def make_separator(y, width):
    box = NSBox.alloc().initWithFrame_(NSMakeRect(0, y, width, 1))
    box.setBoxType_(NSBezelBorder)
    return box


class PopoverController(NSObject):
    def init(self):
        self = super(PopoverController, self).init()
        if self is None:
            return None
        self.devices = []
        self.router_running = False
        self.metro_running = False
        self._poll_timer = None
        return self

    def buildView(self):
        W = 320
        y = 340
        self.view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, y + 20))

        dark = NSColor.textColor()
        gray = NSColor.disabledControlTextColor()

        title = make_label(NSMakeRect(12, y - 4, 200, 20), "Audio Router", 14)
        self.view.addSubview_(title)

        y -= 28
        self.view.addSubview_(make_label(NSMakeRect(12, y, 140, 16), "Full Range:", 11))
        self.view.addSubview_(make_label(NSMakeRect(168, y, 140, 16), "Bass:", 11))
        y -= 22
        self.fullPopup = make_popup_button(
            NSMakeRect(8, y, 148, 22), ["Loading..."], self, "fullDeviceChanged:"
        )
        self.view.addSubview_(self.fullPopup)
        self.bassPopup = make_popup_button(
            NSMakeRect(164, y, 148, 22), ["Loading..."], self, "bassDeviceChanged:"
        )
        self.view.addSubview_(self.bassPopup)

        y -= 30
        self.view.addSubview_(make_separator(y + 10, W - 24))
        y -= 10
        self.view.addSubview_(make_label(NSMakeRect(12, y, 200, 16), "Crossover", 11))
        self.cutoffVal = make_label(NSMakeRect(260, y, 48, 16), "80 Hz", 11)
        self.cutoffVal.setAlignment_(2)
        self.view.addSubview_(self.cutoffVal)
        y -= 18
        self.cutoffSlider = make_slider(
            NSMakeRect(12, y, W - 24, 16), 40, 200, 80, self, "cutoffChanged:"
        )
        self.view.addSubview_(self.cutoffSlider)

        y -= 22
        self.view.addSubview_(make_label(NSMakeRect(12, y, 200, 16), "Sync Delay", 11))
        self.delayVal = make_label(NSMakeRect(260, y, 48, 16), "150 ms", 11)
        self.delayVal.setAlignment_(2)
        self.view.addSubview_(self.delayVal)
        y -= 18
        self.delaySlider = make_slider(
            NSMakeRect(12, y, W - 24, 16), 0, 1000, 150, self, "delayChanged:"
        )
        self.view.addSubview_(self.delaySlider)

        y -= 28
        self.routerBtn = make_button(
            NSMakeRect(12, y, 140, 24), "▶ Start Router", self, "toggleRouter:"
        )
        self.view.addSubview_(self.routerBtn)

        y -= 36
        self.view.addSubview_(make_separator(y + 10, W - 24))
        y -= 10
        self.view.addSubview_(
            make_label(NSMakeRect(12, y, 200, 16), "Metronome BPM", 11)
        )
        self.bpmVal = make_label(NSMakeRect(260, y, 48, 16), "120", 11)
        self.bpmVal.setAlignment_(2)
        self.view.addSubview_(self.bpmVal)
        y -= 18
        self.bpmSlider = make_slider(
            NSMakeRect(12, y, W - 24, 16), 30, 300, 120, self, "bpmChanged:"
        )
        self.view.addSubview_(self.bpmSlider)

        y -= 22
        self.view.addSubview_(make_label(NSMakeRect(12, y, 200, 16), "Full Volume", 11))
        self.fullVolVal = make_label(NSMakeRect(260, y, 48, 16), "80%", 11)
        self.fullVolVal.setAlignment_(2)
        self.view.addSubview_(self.fullVolVal)
        y -= 18
        self.fullVolSlider = make_slider(
            NSMakeRect(12, y, W - 24, 16), 0, 100, 80, self, "fullVolChanged:"
        )
        self.view.addSubview_(self.fullVolSlider)

        y -= 22
        self.view.addSubview_(make_label(NSMakeRect(12, y, 200, 16), "Bass Volume", 11))
        self.bassVolVal = make_label(NSMakeRect(260, y, 48, 16), "80%", 11)
        self.bassVolVal.setAlignment_(2)
        self.view.addSubview_(self.bassVolVal)
        y -= 18
        self.bassVolSlider = make_slider(
            NSMakeRect(12, y, W - 24, 16), 0, 100, 80, self, "bassVolChanged:"
        )
        self.view.addSubview_(self.bassVolSlider)

        y -= 28
        self.metroBtn = make_button(
            NSMakeRect(12, y, 140, 24), "▶ Start Metronome", self, "toggleMetronome:"
        )
        self.view.addSubview_(self.metroBtn)

        return self.view

    def refreshDevices(self):
        result = api_get("/api/devices")
        if result:
            self.devices = result
            names = [d["name"] for d in self.devices]
            if not names:
                names = ["No devices"]
            self.fullPopup.removeAllItems()
            self.fullPopup.addItemsWithTitles_(names)
            self.bassPopup.removeAllItems()
            self.bassPopup.addItemsWithTitles_(names)
            if len(self.devices) >= 2:
                self.fullPopup.selectItemAtIndex_(0)
                self.bassPopup.selectItemAtIndex_(1)

    def refreshStatus(self):
        router = api_get("/api/status")
        metro = api_get("/api/metronome/status")

        if router:
            was_running = self.router_running
            self.router_running = router.get("running", False)
            if self.router_running:
                self.routerBtn.setTitle_("■ Stop Router")
            else:
                self.routerBtn.setTitle_("▶ Start Router")

        if metro:
            self.metro_running = metro.get("running", False)
            if self.metro_running:
                self.metroBtn.setTitle_("■ Stop Metronome")
            else:
                self.metroBtn.setTitle_("▶ Start Metronome")
            if metro.get("bpm"):
                self.bpmSlider.setFloatValue_(metro["bpm"])
                self.bpmVal.setStringValue_(str(int(metro["bpm"])))

    def startPolling(self):
        def poll():
            while True:
                time.sleep(1)
                self.refreshStatus()

        t = threading.Thread(target=poll, daemon=True)
        t.start()

    def fullDeviceChanged_(self, sender):
        pass

    def bassDeviceChanged_(self, sender):
        pass

    def cutoffChanged_(self, sender):
        val = int(sender.floatValue())
        self.cutoffVal.setStringValue_(f"{val} Hz")

    def delayChanged_(self, sender):
        val = int(sender.floatValue())
        self.delayVal.setStringValue_(f"{val} ms")
        api_post("/api/delay", {"delay_ms": val})

    def bpmChanged_(self, sender):
        val = int(sender.floatValue())
        self.bpmVal.setStringValue_(str(val))
        if self.metro_running:
            api_post("/api/metronome/bpm", {"bpm": val})

    def fullVolChanged_(self, sender):
        val = int(sender.floatValue())
        self.fullVolVal.setStringValue_(f"{val}%")

    def bassVolChanged_(self, sender):
        val = int(sender.floatValue())
        self.bassVolVal.setStringValue_(f"{val}%")

    def toggleRouter_(self, sender):
        if self.router_running:
            api_post("/api/stop")
        else:
            full_idx = self.fullPopup.indexOfSelectedItem()
            bass_idx = self.bassPopup.indexOfSelectedItem()
            if full_idx < 0 or bass_idx < 0:
                return
            api_post(
                "/api/start",
                {
                    "full": self.devices[full_idx]["id"],
                    "bass": self.devices[bass_idx]["id"],
                    "cutoff": int(self.cutoffSlider.floatValue()),
                    "delay": int(self.delaySlider.floatValue()),
                    "rate": 48000,
                    "mute": True,
                },
            )
        self.refreshStatus()

    def toggleMetronome_(self, sender):
        if self.metro_running:
            api_post("/api/metronome/stop")
        else:
            full_idx = self.fullPopup.indexOfSelectedItem()
            bass_idx = self.bassPopup.indexOfSelectedItem()
            if full_idx < 0 or bass_idx < 0:
                return
            api_post(
                "/api/metronome/start",
                {
                    "full_device": self.devices[full_idx]["id"],
                    "bass_device": self.devices[bass_idx]["id"],
                    "bpm": int(self.bpmSlider.floatValue()),
                    "full_volume": int(self.fullVolSlider.floatValue()) / 100,
                    "bass_volume": int(self.bassVolSlider.floatValue()) / 100,
                },
            )
        self.refreshStatus()


class MenuBarApp(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        self.popover_controller = None
        self.popover = None

        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
            NSVariableStatusItemLength
        )
        self.status_item.setHighlightMode_(True)

        icon = NSImage.imageNamed_("NSActionTemplate")
        if icon:
            icon.setSize_(NSMakeSize(18, 18))
        self.status_item.setImage_(icon)

        self.popover_controller = PopoverController.alloc().init()
        self.popover_controller.buildView()
        self.popover_controller.refreshDevices()
        self.popover_controller.startPolling()

        self.popover = NSPopover.alloc().init()
        self.popover.setBehavior_(NSPopoverBehaviorTransient)
        self.popover.setAppearance_(NSPopoverAppearanceHUD)
        self.popover.setContentSize_(NSMakeSize(320, 380))
        self.popover.setContentViewController_(self.popover_controller)

        self.status_item.setAction_("togglePopover:")
        self.status_item.setTarget_(self)

    def togglePopover_(self, sender):
        if self.popover.isShown():
            self.popover.performClose_(sender)
        else:
            self.popover_controller.refreshDevices()
            self.popover.showRelativeToRect_ofView_preferredEdge_(
                NSMakeRect(0, 0, 24, 24),
                self.status_item.button(),
                2,
            )


def wait_for_server(base_url, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        try:
            urllib.request.urlopen(f"{base_url}/api/status", timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def start_flask_server():
    app_dir = os.path.dirname(os.path.abspath(__file__))
    flask_app = os.path.join(app_dir, "web", "app.py")
    subprocess.Popen(
        [sys.executable, flask_app],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return wait_for_server(API_BASE)


def main():
    start_flask_server()

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    delegate = MenuBarApp.alloc().init()
    app.setDelegate_(delegate)
    app.finishLaunching()

    NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        delegate,
        "applicationDidFinishLaunching:",
        NSApplicationDidFinishLaunchingNotification,
        None,
    )

    app.run()


if __name__ == "__main__":
    main()

"""The Convertify window: the same kind of window Midify uses, with its own engine behind it.

Deliberately thin. Everything it knows how to do lives in engine.py, and the parts worth sharing
with Midify (the YouTube downloader, keeping that downloader current, showing a file in Finder)
are the same files, not copies.
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "app"))

import objc  # noqa: E402
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,  # noqa: E402
                    NSMakeRect, NSMenu, NSMenuItem, NSScreen, NSTitledWindowMask, NSWindow)
from Foundation import NSObject, NSURL, NSURLRequest  # noqa: E402
from WebKit import WKWebView, WKWebViewConfiguration  # noqa: E402

TITLE = "Convertify"
START_SIZE = (860, 760)
PORT = int(os.environ.get("CONVERTIFY_PORT", "8766"))
URL = f"http://127.0.0.1:{PORT}"


class Delegate(NSObject):
    def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
        return True


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def serve_in_background():
    """Start the engine, and keep it alive if it falls over."""
    import engine
    import uvicorn
    engine.start_background()
    for attempt in range(3):
        try:
            uvicorn.run(engine.app, host="127.0.0.1", port=PORT, log_level="warning")
            return
        except Exception as e:                            # noqa: BLE001
            print(f"the engine stopped ({e}); starting it again", flush=True)
            time.sleep(2 * (attempt + 1))


def build_menu(app):
    """Enough of a menu bar that copy, paste and quit work, which a window needs on a Mac."""
    bar = NSMenu.alloc().init()
    app_item = NSMenuItem.alloc().init()
    bar.addItem_(app_item)
    app_menu = NSMenu.alloc().init()
    app_menu.addItemWithTitle_action_keyEquivalent_(f"Hide {TITLE}", "hide:", "h")
    app_menu.addItem_(NSMenuItem.separatorItem())
    app_menu.addItemWithTitle_action_keyEquivalent_(f"Quit {TITLE}", "terminate:", "q")
    app_item.setSubmenu_(app_menu)

    edit_item = NSMenuItem.alloc().init()
    bar.addItem_(edit_item)
    edit = NSMenu.alloc().initWithTitle_("Edit")
    for name, action, key in (("Cut", "cut:", "x"), ("Copy", "copy:", "c"),
                              ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")):
        edit.addItemWithTitle_action_keyEquivalent_(name, action, key)
    edit_item.setSubmenu_(edit)
    app.setMainMenu_(bar)


def main():
    if port_in_use(PORT):
        print(f"{TITLE} is already open.")
    else:
        threading.Thread(target=serve_in_background, daemon=True).start()

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    delegate = Delegate.alloc().init()
    app.setDelegate_(delegate)
    build_menu(app)

    screen = NSScreen.mainScreen().visibleFrame()
    width = min(START_SIZE[0], screen.size.width - 80)
    height = min(START_SIZE[1], screen.size.height - 80)
    rect = NSMakeRect(screen.origin.x + (screen.size.width - width) / 2,
                      screen.origin.y + (screen.size.height - height) / 2, width, height)
    style = NSTitledWindowMask | (1 << 1) | (1 << 2) | (1 << 3)      # close, minimise, resize
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, style, NSBackingStoreBuffered, False)
    window.setTitle_(TITLE)

    web = WKWebView.alloc().initWithFrame_configuration_(rect, WKWebViewConfiguration.alloc().init())
    web.setAutoresizingMask_((1 << 1) | (1 << 4))
    window.setContentView_(web)
    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    def show_when_ready():
        for _ in range(120):
            if port_in_use(PORT):
                break
            time.sleep(0.5)
        from PyObjCTools import AppHelper
        AppHelper.callAfter(lambda: web.loadRequest_(
            NSURLRequest.requestWithURL_(NSURL.URLWithString_(URL))))

    threading.Thread(target=show_when_ready, daemon=True).start()
    from PyObjCTools import AppHelper
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()

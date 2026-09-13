"""The Mac app: a real window, not a browser tab.

The engine runs inside this process and the window shows its pages through macOS's
own web view, the same way Slack and VS Code are built. What that buys over opening
a browser: an icon in the Dock, a proper menu bar, Cmd+Q, a real file picker, real
saves into your Downloads folder, and a window that remembers its size.

    python app/desktop.py
"""
import os
import sys
import threading
import time
import urllib.request

APP = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP)

import objc  # noqa: E402
from AppKit import (NSApplication, NSApplicationActivationPolicyRegular, NSBackingStoreBuffered,  # noqa: E402
                    NSMakeRect, NSMenu, NSMenuItem, NSModalResponseOK, NSOpenPanel, NSScreen,
                    NSWindow, NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable,
                    NSWindowStyleMaskResizable, NSWindowStyleMaskTitled, NSWorkspace)
from Foundation import NSObject, NSURL, NSURLRequest  # noqa: E402
from WebKit import WKWebView, WKWebViewConfiguration  # noqa: E402

TITLE = "Clavinova MIDI Maker"
MIN_SIZE = (820, 620)
START_SIZE = (1060, 820)

ALLOW, DOWNLOAD = 1, 2                      # WKNavigationResponsePolicy


def unique_path(folder, name):
    base, ext = os.path.splitext(name or "song.mid")
    path = os.path.join(folder, base + ext)
    n = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base} {n}{ext}")
        n += 1
    return path


class Delegate(NSObject):
    """Window, page and download behaviour. One object, because they all belong together."""

    def initWithServer_(self, base_url):
        self = objc.super(Delegate, self).init()
        self.base_url = base_url
        return self

    # The app quits when its window closes, like any single window Mac app.
    def applicationShouldTerminateAfterLastWindowClosed_(self, sender):
        return True

    # "Choose a file" opens the real macOS panel.
    def webView_runOpenPanelWithParameters_initiatedByFrame_completionHandler_(
            self, web_view, params, frame, handler):
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(params.allowsMultipleSelection())
        panel.setTitle_("Choose a recording")
        panel.setPrompt_("Use this")
        handler(panel.URLs() if panel.runModal() == NSModalResponseOK else None)

    # A MIDI file is not something to display, so it becomes a download instead.
    def webView_decidePolicyForNavigationResponse_decisionHandler_(self, web_view, response, handler):
        handler(ALLOW if response.canShowMIMEType() else DOWNLOAD)

    def webView_navigationResponse_didBecomeDownload_(self, web_view, response, download):
        download.setDelegate_(self)

    def webView_navigationAction_didBecomeDownload_(self, web_view, action, download):
        download.setDelegate_(self)

    def download_decideDestinationUsingResponse_suggestedFilename_completionHandler_(
            self, download, response, filename, handler):
        downloads = os.path.expanduser("~/Downloads")
        self.last_download = unique_path(downloads, str(filename))
        handler(NSURL.fileURLWithPath_(self.last_download))

    def downloadDidFinish_(self, download):
        path = getattr(self, "last_download", None)
        if path and os.path.exists(path):       # show it, the way a Mac app does
            NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(path)])

    def download_didFailWithError_resumeData_(self, download, error, resume_data):
        pass


def claim_identity():
    """Without this the menu bar says "python".

    The program that actually runs is the Python binary, which lives outside the app, so
    AppKit reads its identity from there. Patching the main bundle's info dictionary before
    the application starts is the documented way to make the menu bar use the app's name.
    """
    from Foundation import NSBundle, NSProcessInfo
    bundle = NSBundle.mainBundle()
    info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
    if info is not None:
        info["CFBundleName"] = TITLE
        info["CFBundleDisplayName"] = TITLE
        info["CFBundleIdentifier"] = "com.koderking25.clavinova-midi-maker"
    try:
        NSProcessInfo.processInfo().setProcessName_(TITLE)
    except Exception:  # noqa: BLE001
        pass


def set_dock_icon(app):
    """Same reason: without this the Dock shows Python's icon instead of the app's."""
    from AppKit import NSImage
    resources = os.path.dirname(APP)                       # Resources/ inside the bundle
    for candidate in (os.path.join(resources, "icon.icns"),
                      os.path.join(os.path.dirname(APP), "mac", "icon.icns")):   # running from the repo
        if os.path.exists(candidate):
            image = NSImage.alloc().initWithContentsOfFile_(candidate)
            if image is not None:
                app.setApplicationIconImage_(image)
                return candidate
    return None


def build_menu(app):
    """Without this there is no Cmd+Q, and no copy or paste inside the window."""
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
    edit_menu = NSMenu.alloc().initWithTitle_("Edit")
    for title, action, key in (("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), (None, None, None),
                               ("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
                               ("Select All", "selectAll:", "a")):
        if title is None:
            edit_menu.addItem_(NSMenuItem.separatorItem())
        else:
            edit_menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
    edit_item.setSubmenu_(edit_menu)

    window_item = NSMenuItem.alloc().init()
    bar.addItem_(window_item)
    window_menu = NSMenu.alloc().initWithTitle_("Window")
    window_menu.addItemWithTitle_action_keyEquivalent_("Minimize", "performMiniaturize:", "m")
    window_menu.addItemWithTitle_action_keyEquivalent_("Zoom", "performZoom:", "")
    window_item.setSubmenu_(window_menu)
    app.setWindowsMenu_(window_menu)

    app.setMainMenu_(bar)


def serve_in_background():
    """Run the engine inside this app. uvicorn wants the main thread for signal handling,
    which belongs to the window, so that part is switched off."""
    import uvicorn
    import pipeline
    import server

    server.clean_leftovers()
    threading.Thread(target=pipeline.check_models_in_child, daemon=True).start()
    threading.Thread(target=server.worker, daemon=True).start()
    config = uvicorn.Config(server.app, host="127.0.0.1", port=server.PORT, log_level="warning")
    running = uvicorn.Server(config)
    running.install_signal_handlers = lambda: None
    running.run()


def wait_for_server(url, seconds=90):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/status", timeout=2):
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    return False


def already_running(url):
    try:
        with urllib.request.urlopen(url + "/api/status", timeout=2):
            return True
    except Exception:  # noqa: BLE001
        return False


def main():
    import server
    url = f"http://127.0.0.1:{server.PORT}"

    if not already_running(url):                    # a second copy just shows the first one's pages
        threading.Thread(target=serve_in_background, daemon=True).start()
        if not wait_for_server(url):
            sys.exit("The engine did not start. See ~/Library/Logs/Clavinova MIDI Maker.log")

    claim_identity()                                  # must happen before the application exists
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    icon_path = set_dock_icon(app)
    delegate = Delegate.alloc().initWithServer_(url)
    app.setDelegate_(delegate)
    build_menu(app)

    screen = NSScreen.mainScreen().visibleFrame()
    width = min(START_SIZE[0], screen.size.width - 80)
    height = min(START_SIZE[1], screen.size.height - 80)
    rect = NSMakeRect(screen.origin.x + (screen.size.width - width) / 2,
                      screen.origin.y + (screen.size.height - height) / 2, width, height)
    style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
             | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(rect, style, NSBackingStoreBuffered, False)
    window.setTitle_(TITLE)
    window.setMinSize_((MIN_SIZE[0], MIN_SIZE[1]))
    window.setFrameAutosaveName_("ClavinovaMainWindow")      # remembers size and position

    config = WKWebViewConfiguration.alloc().init()
    web = WKWebView.alloc().initWithFrame_configuration_(window.contentView().bounds(), config)
    web.setAutoresizingMask_(1 << 1 | 1 << 4)                # follows the window when resized
    web.setUIDelegate_(delegate)
    web.setNavigationDelegate_(delegate)
    web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
    window.contentView().addSubview_(web)

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    # Written to ~/Library/Logs/Clavinova MIDI Maker.log so the app can be checked
    # without watching the screen.
    from Foundation import NSBundle
    info = NSBundle.mainBundle().infoDictionary() or {}
    print(f"window open: title={window.title()!r} size={int(width)}x{int(height)} "
          f"visible={bool(window.isVisible())}", flush=True)
    print(f"identity: menu bar name={info.get('CFBundleName')!r}, dock icon={icon_path}", flush=True)
    app.run()


if __name__ == "__main__":
    main()

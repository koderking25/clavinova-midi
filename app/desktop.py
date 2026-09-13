"""The Mac app: a real window, not a browser tab.

The engine runs inside this process and the window shows its pages through macOS's
own web view, the same way Slack and VS Code are built. What that buys over opening
a browser: an icon in the Dock, a proper menu bar, Cmd+Q, a real file picker, real
saves into your Downloads folder, and a window that remembers its size.

    python app/desktop.py
"""
import time

STARTED = time.time()                        # for the "window shown after" line in the log

import os  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import urllib.request  # noqa: E402

APP = os.path.dirname(os.path.abspath(__file__))

# Inside the .app, compiled Python must not be written next to the code: added files break the
# app's signature. The launcher already sets this; this covers any other way of starting it,
# before a single app module is imported. The environment copy reaches the song processes too.
if ".app/Contents/" in APP and not os.environ.get("PYTHONPYCACHEPREFIX"):
    _cache = os.path.expanduser("~/Library/Application Support/Clavinova MIDI Maker/pycache")
    os.environ["PYTHONPYCACHEPREFIX"] = _cache
    sys.pycache_prefix = _cache

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

    # Links to the outside world (BitMidi, GitHub) open in your browser. Without this they
    # loaded inside the app's own window, with no way back to the app.
    def webView_decidePolicyForNavigationAction_decisionHandler_(self, web_view, action, handler):
        url = action.request().URL()
        if url is not None and url.scheme() in ("http", "https") and url.host() not in ("127.0.0.1", "localhost"):
            NSWorkspace.sharedWorkspace().openURL_(url)
            handler(0)                                   # cancel here: it opened in the browser
        else:
            handler(1)                                   # the app's own pages load normally

    def webView_createWebViewWithConfiguration_forNavigationAction_windowFeatures_(
            self, web_view, configuration, action, features):
        url = action.request().URL()                     # links that ask for a new window
        if url is not None:
            NSWorkspace.sharedWorkspace().openURL_(url)
        return None

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

    import updater
    server.clean_leftovers()
    threading.Thread(target=pipeline.check_models_in_child, daemon=True).start()
    threading.Thread(target=server.worker, daemon=True).start()
    threading.Thread(target=updater.startup, daemon=True).start()
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


LOADING_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>
  /* Same colours and piano keys banner as the app, so the switch to it is seamless. */
  :root { color-scheme: light dark; --bg: #f5f1e8; --ink: #1d1b17; --muted: #6c655a; --line: #e3dac9;
          --accent: #9b2c1f; --key-w: #fffdf8; --key-b: #1d1b17; }
  @media (prefers-color-scheme: dark) { :root { --bg: #171512; --ink: #f1ece3; --muted: #a79f92; --line: #3a352e;
          --accent: #e0695a; --key-w: #e9e3d8; --key-b: #0d0c0a; } }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--ink);
               font: 15px -apple-system, BlinkMacSystemFont, system-ui, sans-serif; }
  .keys { height: 26px; background: repeating-linear-gradient(90deg, transparent 0 21px, var(--line) 21px 22px), var(--key-w);
          position: relative; overflow: hidden; border-bottom: 1px solid var(--line); }
  .keys::after { content: ""; position: absolute; inset: 0 0 9px 0; background: repeating-linear-gradient(90deg,
    transparent 0 15px, var(--key-b) 15px 28px, transparent 28px 37px, var(--key-b) 37px 50px, transparent 50px 81px,
    var(--key-b) 81px 94px, transparent 94px 103px, var(--key-b) 103px 116px, transparent 116px 125px,
    var(--key-b) 125px 138px, transparent 138px 154px); }
  main { height: calc(100% - 27px); display: grid; place-content: center; text-align: center; gap: 14px; }
  h1 { font: 600 26px Georgia, "Times New Roman", serif; margin: 0; }
  p { margin: 0; color: var(--muted); }
  .bar { width: 220px; height: 4px; margin: 6px auto 0; border-radius: 4px; overflow: hidden; background: rgba(128,128,128,.25); }
  .bar i { display: block; width: 40%; height: 100%; background: var(--accent); border-radius: 4px; animation: slide 1.1s ease-in-out infinite; }
  @keyframes slide { from { transform: translateX(-100%); } to { transform: translateX(250%); } }
  .err { color: var(--accent); max-width: 420px; line-height: 1.5; }
</style></head><body><div class="keys"></div>
<main><h1>Clavinova MIDI Maker</h1><p id="msg">Starting up&hellip;</p><div class="bar" id="bar"><i></i></div></main>
</body></html>"""


def show_problem(web, text):
    """Replace the loading screen with a plain explanation, instead of quitting silently."""
    import json
    js = (f"document.getElementById('msg').className='err';"
          f"document.getElementById('msg').textContent={json.dumps(text)};"
          f"document.getElementById('bar').remove();")
    web.evaluateJavaScript_completionHandler_(js, None)


def start_engine(web, url):
    """Runs off the main thread: start the engine, then point the window at it."""
    from PyObjCTools import AppHelper
    if not already_running(url):                      # a second copy just uses the first one's engine
        threading.Thread(target=serve_in_background, daemon=True).start()
    if wait_for_server(url):
        print(f"engine ready after {time.time() - STARTED:.1f} s", flush=True)
        AppHelper.callAfter(web.loadRequest_, NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
    else:
        print("the engine did not start within 90 s", flush=True)
        AppHelper.callAfter(show_problem, web, "The app could not start its engine. Quit it with Cmd+Q and "
                            "open it again. If it keeps happening, the details are in "
                            "~/Library/Logs/Clavinova MIDI Maker.log")


def main():
    # The window comes first and the engine loads behind it. Opening used to wait for the whole
    # engine before anything appeared, and a Mac that is busy (just restarted, or scanning the
    # app) turned that silence into "the application is not responding".
    port = int(os.environ.get("CLAVINOVA_PORT", "8765"))
    url = f"http://127.0.0.1:{port}"

    claim_identity()                                  # must happen before the application exists
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    icon_path = set_dock_icon(app)
    delegate = Delegate.alloc().initWithServer_(url)
    app.setDelegate_(delegate)
    build_menu(app)

    # The updater quits the app this way, on the main thread, so its swap script can take over.
    import updater
    from PyObjCTools import AppHelper
    updater.UPDATER.quit_hook = lambda: AppHelper.callAfter(app.terminate_, None)

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
    try:
        web.setValue_forKey_(False, "drawsBackground")      # no white flash between screens in dark mode
    except Exception:  # noqa: BLE001
        pass
    web.loadHTMLString_baseURL_(LOADING_PAGE, None)
    window.contentView().addSubview_(web)

    window.makeKeyAndOrderFront_(None)
    app.activateIgnoringOtherApps_(True)

    # Written to ~/Library/Logs/Clavinova MIDI Maker.log so the app can be checked
    # without watching the screen.
    from Foundation import NSBundle
    info = NSBundle.mainBundle().infoDictionary() or {}
    print(f"window open: title={window.title()!r} size={int(width)}x{int(height)} "
          f"visible={bool(window.isVisible())}", flush=True)
    print(f"window shown after {time.time() - STARTED:.1f} s", flush=True)
    print(f"identity: menu bar name={info.get('CFBundleName')!r}, dock icon={icon_path}", flush=True)
    threading.Thread(target=start_engine, args=(web, url), daemon=True).start()
    app.run()


if __name__ == "__main__":
    main()

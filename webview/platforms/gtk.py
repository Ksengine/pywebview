"""
(C) 2014-2019 Roman Sirokov and contributors
Licensed under BSD license

http://github.com/r0x0r/pywebview/
"""
import sys
import logging
import json
import webbrowser
try:
    from urllib.parse import unquote
except ImportError:
    from urllib import unquote

from uuid import uuid1
from threading import Event, Semaphore, Thread
from multiprocessing import Process, Queue, Pipe
from webview.localization import localization
from webview import _debug, _user_agent, OPEN_DIALOG, FOLDER_DIALOG, SAVE_DIALOG, parse_file_type, escape_string, windows, _multiprocessing, Window
from webview.util import parse_api_js, default_html, js_bridge_call
from webview.js.css import disable_text_select
from webview.serving import resolve_url

logger = logging.getLogger('pywebview')

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('WebKit2', '4.0')

from gi.repository import Gtk as gtk
from gi.repository import Gdk
from gi.repository import GLib as glib
from gi.repository import WebKit2 as webkit


# version of WebKit2 older than 2.2 does not support returning a result of javascript, so we
# have to resort fetching a result via window title
webkit_ver = webkit.get_major_version(), webkit.get_minor_version(), webkit.get_micro_version()
old_webkit = webkit_ver[0] < 2 or webkit_ver[1] < 22

renderer = 'gtkwebkit2'

settings = {}

class BrowserView:
    instances = {}

    class JSBridge:
        def __init__(self, window):
            self.window = window
            self.uid = uuid1().hex[:8]

        def call(self, func_name, param, value_id):
            if param == 'undefined':
                param = None
            return js_bridge_call(self.window, func_name, param, value_id)

    def __init__(self, window):
        BrowserView.instances[window.uid] = self
        self.uid = window.uid
        self.pywebview_window = window

        self.is_fullscreen = False
        self.js_results = {}

        glib.threads_init()
        self.window = gtk.Window(title=window.title)

        self.shown = window.shown
        self.loaded = window.loaded

        if window.resizable:
            self.window.set_size_request(window.min_size[0], window.min_size[1])
            self.window.resize(window.initial_width, window.initial_height)
        else:
            self.window.set_size_request(window.initial_width, window.initial_height)

        if window.minimized:
            self.window.iconify()

        if window.initial_x is not None and window.initial_y is not None:
            self.move(window.initial_x, window.initial_y)
        else:
            self.window.set_position(gtk.WindowPosition.CENTER)

        self.window.set_resizable(window.resizable)

        # Set window background color
        style_provider = gtk.CssProvider()
        style_provider.load_from_data(
            'GtkWindow {{ background-color: {}; }}'.format(window.background_color).encode()
        )
        gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            style_provider,
            gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        scrolled_window = gtk.ScrolledWindow()
        self.window.add(scrolled_window)

        if window.confirm_close:
            self.window.connect('delete-event', self.on_destroy)
        else:
            self.window.connect('delete-event', self.close_window)

        self.js_bridge = BrowserView.JSBridge(window)
        self.text_select = window.text_select

        self.webview = webkit.WebView()
        self.webview.connect('notify::visible', self.on_webview_ready)
        self.webview.connect('load_changed', self.on_load_finish)
        self.webview.connect('notify::title', self.on_title_change)
        self.webview.connect('decide-policy', self.on_navigation)

        user_agent = settings.get('user_agent') or _user_agent
        if user_agent:
            self.webview.get_settings().props.user_agent = user_agent

        if window.frameless:
            self.window.set_decorated(False)
            if window.easy_drag:
                self.move_progress = False
                self.webview.connect('button-release-event', self.on_mouse_release)
                self.webview.connect('button-press-event', self.on_mouse_press)
                self.window.connect('motion-notify-event', self.on_mouse_move)

        if window.on_top:
            self.window.set_keep_above(True)

        self.transparent = window.transparent
        if window.transparent:
            configure_transparency(self.window)
            configure_transparency(self.webview)
            wvbg = self.webview.get_background_color()
            wvbg.alpha = 0.0
            self.webview.set_background_color(wvbg)

        if _debug:
            self.webview.get_settings().props.enable_developer_extras = True
        else:
            self.webview.connect('context-menu', lambda a,b,c,d: True) # Disable context menu

        self.webview.set_opacity(0.0)
        scrolled_window.add(self.webview)

        if window.real_url is not None:
            self.webview.load_uri(window.real_url)
        elif window.html:
            self.webview.load_html(window.html, '')
        else:
            self.webview.load_html(default_html, '')

        if window.fullscreen:
            self.toggle_fullscreen()

    def close_window(self, *data):
        self.pywebview_window.closing.set()

        for res in self.js_results.values():
            res['semaphore'].release()

        while gtk.events_pending():
            gtk.main_iteration()

        self.window.destroy()
        del BrowserView.instances[self.uid]

        if self.pywebview_window in windows:
            windows.remove(self.pywebview_window)

        self.pywebview_window.closed.set()

        if BrowserView.instances == {}:
            gtk.main_quit()

    def on_destroy(self, widget=None, *data):
        dialog = gtk.MessageDialog(parent=self.window, flags=gtk.DialogFlags.MODAL & gtk.DialogFlags.DESTROY_WITH_PARENT,
                                          type=gtk.MessageType.QUESTION, buttons=gtk.ButtonsType.OK_CANCEL,
                                          message_format=localization['global.quitConfirmation'])
        result = dialog.run()
        if result == gtk.ResponseType.OK:
            self.close_window()

        dialog.destroy()
        return True

    def on_webview_ready(self, arg1, arg2):
        # in webkit2 notify:visible fires after the window was closed and BrowserView object destroyed.
        # for a lack of better solution we check that BrowserView has 'webview_ready' attribute
        if 'shown' in dir(self):
            self.shown.set()


    def on_load_finish(self, webview, status):
        # Show the webview if it's not already visible
        if not webview.props.opacity:
            glib.idle_add(webview.set_opacity, 1.0)

        if status == webkit.LoadEvent.FINISHED:
            if not self.text_select:
                webview.run_javascript(disable_text_select)
            self._set_js_api()

    def on_title_change(self, webview, title):
        title = webview.get_title()

        try:
            js_data = json.loads(title)

            if 'type' not in js_data:
                return

            elif js_data['type'] == 'eval' and old_webkit:  # return result of evaluate_js
                unique_id = js_data['uid']
                result = js_data['result'] if 'result' in js_data else None

                js = self.js_results[unique_id]
                js['result'] = result
                js['semaphore'].release()

            elif js_data['type'] == 'invoke':  # invoke js api's function
                func_name = js_data['function']
                value_id = js_data['id']
                param = js_data['param'] if 'param' in js_data else None
                return_val = self.js_bridge.call(func_name, param, value_id)

                # Give back the return value to JS as a string
                code = 'pywebview._bridge.return_val = "{0}";'.format(escape_string(str(return_val)))
                webview.run_javascript(code)

        except ValueError: # Python 2
            logger.debug('GTK: JSON decode failed:\n %s' % title)
        except json.JSONDecodeError: # Python 3
            logger.debug('GTK: JSON decode failed:\n %s' % title)

    def on_navigation(self, webview, decision, decision_type):
        if type(decision) == webkit.NavigationPolicyDecision:
            uri = decision.get_request().get_uri()

            if decision.get_frame_name() == '_blank':
                webbrowser.open(uri, 2, True)
                decision.ignore()

    def on_mouse_release(self, sender, event):
        self.move_progress = False

    def on_mouse_press(self, _, event):
        self.point_diff = [x - y for x, y in zip(self.window.get_position(), [event.x_root, event.y_root])]
        self.move_progress = True

    def on_mouse_move(self, _, event):
        if self.move_progress:
            point = [x + y for x, y in zip((event.x_root, event.y_root), self.point_diff)]
            self.window.move(point[0], point[1])

    def show(self):
        self.window.show_all()

        if gtk.main_level() == 0:
            if self.pywebview_window.hidden:
                self.window.hide()

            gtk.main()
        else:
            glib.idle_add(self.window.show_all)

    def hide(self):
        glib.idle_add(self.window.hide)

    def destroy(self):
        self.window.emit('delete-event', Gdk.Event())

    def set_title(self, title):
        self.window.set_title(title)

    def toggle_fullscreen(self):
        if self.is_fullscreen:
            self.window.unfullscreen()
        else:
            self.window.fullscreen()

        self.is_fullscreen = not self.is_fullscreen

    def resize(self, width, height):
        self.window.resize(width, height)

    def move(self, x, y):
        self.window.move(x, y)

    def minimize(self):
        glib.idle_add(self.window.iconify)

    def restore(self):
        def _restore():
            self.window.deiconify()
            self.window.present()

        glib.idle_add(_restore)

    def create_file_dialog(self, dialog_type, directory, allow_multiple, save_filename, file_types):
        if dialog_type == FOLDER_DIALOG:
            gtk_dialog_type = gtk.FileChooserAction.SELECT_FOLDER
            title = localization['linux.openFolder']
            button = gtk.STOCK_OPEN
        elif dialog_type == OPEN_DIALOG:
            gtk_dialog_type = gtk.FileChooserAction.OPEN
            if allow_multiple:
                title = localization['linux.openFiles']
            else:
                title = localization['linux.openFile']

            button = gtk.STOCK_OPEN
        elif dialog_type == SAVE_DIALOG:
            gtk_dialog_type = gtk.FileChooserAction.SAVE
            title = localization['global.saveFile']
            button = gtk.STOCK_SAVE

        dialog = gtk.FileChooserDialog(title, self.window, gtk_dialog_type,
                                       (gtk.STOCK_CANCEL, gtk.ResponseType.CANCEL, button, gtk.ResponseType.OK))

        dialog.set_select_multiple(allow_multiple)
        dialog.set_current_folder(directory)
        self._add_file_filters(dialog, file_types)

        if dialog_type == SAVE_DIALOG:
            dialog.set_current_name(save_filename)

        response = dialog.run()

        if response == gtk.ResponseType.OK:
            if dialog_type == SAVE_DIALOG:
                file_name = dialog.get_filename()
            else:
                file_name = dialog.get_filenames()
        else:
            file_name = None

        dialog.destroy()

        return file_name

    def _add_file_filters(self, dialog, file_types):
        for s in file_types:
            description, extensions = parse_file_type(s)

            f = gtk.FileFilter()
            f.set_name(description)
            for e in extensions.split(';'):
                f.add_pattern(e)

            dialog.add_filter(f)

    def get_current_url(self):
        self.loaded.wait()
        uri = self.webview.get_uri()
        return uri if uri != 'about:blank' else None

    def load_url(self, url):
        self.loaded.clear()
        self.webview.load_uri(url)

    def load_html(self, content, base_uri):
        self.loaded.clear()
        self.webview.load_html(content, base_uri)

    def evaluate_js(self, script):
        def _evaluate_js():
            callback = None if old_webkit else _callback
            self.webview.run_javascript(script, None, callback, None)

        def _callback(webview, task, data):
            value = webview.run_javascript_finish(task)
            if value:
                self.js_results[unique_id]['result'] = value.get_js_value().to_string()
            else:
                self.js_results[unique_id]['result'] = None

            result_semaphore.release()

        unique_id = uuid1().hex
        result_semaphore = Semaphore(0)
        self.js_results[unique_id] = {'semaphore': result_semaphore, 'result': None}

        if old_webkit:
            script = 'document.title = JSON.stringify({{"type": "eval", "uid": "{0}", "result": {1}}})'.format(unique_id, script)

        self.loaded.wait()
        glib.idle_add(_evaluate_js)
        result_semaphore.acquire()

        if not gtk.main_level():
            # Webview has been closed, don't proceed
            return None

        result = self.js_results[unique_id]['result']
        result = None if result == 'undefined' or result == 'null' or result is None else result if result == '' else json.loads(result)

        del self.js_results[unique_id]

        return result

    def _set_js_api(self):
        def create_bridge():
            self.webview.run_javascript(parse_api_js(self.js_bridge.window, 'gtk', uid=self.js_bridge.uid))
            self.loaded.set()

        glib.idle_add(create_bridge)


def proc_func(window):
    def childs():
        while 1:
            window = Window(*views.get())
            window.loaded._initialize(False)
            window.shown._initialize(False)
            window.real_url = resolve_url(window.original_url, False)
            _create_window(window)
            window.closed += lambda: mp_events.put((window.uid, 'closed'))
            window.closing += lambda: mp_events.put((window.uid, 'closing'))
            window.shown += lambda: mp_events.put((window.uid, 'shown'))
            window.loaded += lambda: mp_events.put((window.uid, 'loaded'))

    def caller():
        while 1:
            data = mp_idle_add.get()
            data[0](*data[1:])

    t = Thread(target=childs)
    t.daemon = True
    t.start()
    t2 = Thread(target=caller)
    t2.daemon = True
    t2.start()
    _create_window(window)


def mp_event_loop():
    while 1:
        uid, event_name = mp_events.get()
        for window in windows:
            if window.uid == uid:
                getattr(window, event_name).set()
                continue


def _create_window(window):
    def create():
        browser = BrowserView(window)
        browser.show()

    if window.uid == 'master':
        create()
    else:
        glib.idle_add(create)


def create_window(window):
    def create():
        browser = BrowserView(window)
        browser.show()

    if not _multiprocessing:
        _create_window(window)
    if window.uid == 'master':
        global views
        views = Queue()
        global mp_events
        mp_events = Queue()
        global mp_idle_add
        mp_idle_add = Queue()
        p = Process(target=proc_func, args=(window, ))
        p.start()
        return p
    else:
        print(window.original_url, window.html)
        views.put(
            (window.uid, window.title, window.original_url, window.html,
             window.initial_width, window.initial_height, window.initial_x,
             window.initial_y, window.resizable, window.fullscreen,
             window.min_size, window.hidden, window.frameless,
             window.easy_drag, window.minimized, window.on_top,
             window.confirm_close, window.background_color, window._js_api,
             window.text_select, window.transparent))
        t = Thread(target=mp_event_loop)
        t.daemon = True
        t.start()


def set_title(title, uid):
    if _multiprocessing:
        mp_idle_add.put((_set_title, title, uid))
    else:
        _set_title(title, uid)


def _set_title(title, uid):
    def _set_title():
        BrowserView.instances[uid].set_title(title)

    glib.idle_add(_set_title)


def destroy_window(uid):
    if _multiprocessing:
        mp_idle_add.put((_destroy_window, uid))
    else:
        _destroy_window(uid)


def _destroy_window(uid):
    def _destroy_window():
        BrowserView.instances[uid].close_window()

    glib.idle_add(_destroy_window)


def toggle_fullscreen(uid):
    if _multiprocessing:
        mp_idle_add.put((_toggle_fullscreen, uid))
    else:
        _toggle_fullscreen(uid)


def _toggle_fullscreen(uid):
    def _toggle_fullscreen():
        BrowserView.instances[uid].toggle_fullscreen()

    glib.idle_add(_toggle_fullscreen)


def set_on_top(uid, top):
    if _multiprocessing:
        mp_idle_add.put((_set_on_top, uid, top))
    else:
        _set_on_top(uid, top)


def _set_on_top(uid, top):
    def _set_on_top():
        BrowserView.instances[uid].window.set_keep_above(top)

    glib.idle_add(_set_on_top)


def resize(width, height, uid):
    if _multiprocessing:
        mp_idle_add.put((_resize, width, height, uid))
    else:
        _resize(width, height, uid)


def _resize(width, height, uid):
    def _resize():
        BrowserView.instances[uid].resize(width, height)

    glib.idle_add(_resize)


def move(x, y, uid):
    if _multiprocessing:
        mp_idle_add.put((_move, x, y, uid))
    else:
        _move(title, uid)


def _move(x, y, uid):
    def _move():
        BrowserView.instances[uid].move(x, y)

    glib.idle_add(_move)


def hide(uid):
    if _multiprocessing:
        mp_idle_add.put((_hide, uid))
    else:
        _hide(uid)


def _hide(uid):
    glib.idle_add(BrowserView.instances[uid].hide)


def show(uid):
    if _multiprocessing:
        mp_idle_add.put((_show, uid))
    else:
        _show(uid)


def _show(uid):
    glib.idle_add(BrowserView.instances[uid].show)


def minimize(uid):
    if _multiprocessing:
        mp_idle_add.put((_minimize, uid))
    else:
        _minimize(uid)


def _minimize(uid):
    glib.idle_add(BrowserView.instances[uid].minimize)


def restore(uid):
    if _multiprocessing:
        mp_idle_add.put((_restore, uid))
    else:
        _restore(uid)


def _restore(uid):
    glib.idle_add(BrowserView.instances[uid].restore)


def get_current_url(uid):
    return BrowserView.instances[uid].get_current_url()


def load_url(url, uid):
    if _multiprocessing:
        mp_idle_add.put((_load_url, url, uid))
    else:
        _load_url(url, uid)


def _load_url(url, uid):
    def _load_url():
        BrowserView.instances[uid].load_url(url)

    glib.idle_add(_load_url)


def load_html(content, base_uri, uid):
    if _multiprocessing:
        mp_idle_add.put((_load_html, content, base_uri, uid))
    else:
        _load_html(content, base_uri, uid)


def _load_html(content, base_uri, uid):
    def _load_html():
        BrowserView.instances[uid].load_html(content, base_uri)

    glib.idle_add(_load_html)


def _create_file_dialog(dialog_type, directory, allow_multiple, save_filename,
                        file_types, uid, child_conn):
    result = BrowserView.instances[uid].create_file_dialog(
        dialog_type, directory, allow_multiple, save_filename, file_types)
    if result is None:
        child_conn.send(None)
    else:
        result = map(unicode, result) if sys.version < '3' else result
        child_conn.send(tuple(result))


def create_file_dialog(dialog_type, directory, allow_multiple, save_filename,
                       file_types, uid):

    parent_conn, child_conn = Pipe(False)
    if _multiprocessing:
        mp_idle_add.put(
            (glib.idle_add, _create_file_dialog, dialog_type, directory,
             allow_multiple, save_filename, file_types, uid, child_conn))
    else:
        glib.idle_add(_create_file_dialog, dialog_type, directory,
                      allow_multiple, save_filename, file_types, uid,
                      child_conn)

    return parent_conn.recv()


def evaluate_js(script, uid):
    return BrowserView.instances[uid].evaluate_js(script)


def _get_position(uid, child_conn):
    child_conn.send(BrowserView.instances[uid].window.get_position())


def get_position(uid):
    parent_conn, child_conn = Pipe(False)
    if _multiprocessing:
        try:
            mp_idle_add.put((_get_position, uid, child_conn))
        except:
            mp_idle_add.put((glib.idle_add, _get_position, uid, child_conn))
    else:
        try:
            _get_position(uid, child_conn)
        except:
            glib.idle_add(_get_position, uid, child_conn)
    return parent_conn.recv()


def _get_size(uid, child_conn):
    child_conn.send(BrowserView.instances[uid].window.get_size())


def get_size(uid):
    parent_conn, child_conn = Pipe(False)
    if _multiprocessing:
        try:
            mp_idle_add.put((_get_size, uid, child_conn))
        except:
            mp_idle_add.put((glib.idle_add, _get_size, uid, child_conn))
    else:
        try:
            _get_size(uid, child_conn)
        except:
            glib.idle_add(_get_size, uid, child_conn)

    return parent_conn.recv()


def configure_transparency(c):
    c.set_visual(c.get_screen().get_rgba_visual())
    c.override_background_color(gtk.StateFlags.ACTIVE, Gdk.RGBA(0, 0, 0, 0))
    c.override_background_color(gtk.StateFlags.BACKDROP, Gdk.RGBA(0, 0, 0, 0))
    c.override_background_color(gtk.StateFlags.DIR_LTR, Gdk.RGBA(0, 0, 0, 0))
    c.override_background_color(gtk.StateFlags.DIR_RTL, Gdk.RGBA(0, 0, 0, 0))
    c.override_background_color(gtk.StateFlags.FOCUSED, Gdk.RGBA(0, 0, 0, 0))
    c.override_background_color(gtk.StateFlags.INCONSISTENT, Gdk.RGBA(0, 0, 0, 0))
    c.override_background_color(gtk.StateFlags.INSENSITIVE, Gdk.RGBA(0, 0, 0, 0))
    c.override_background_color(gtk.StateFlags.NORMAL, Gdk.RGBA(0, 0, 0, 0))
    c.override_background_color(gtk.StateFlags.PRELIGHT, Gdk.RGBA(0, 0, 0, 0))
    c.override_background_color(gtk.StateFlags.SELECTED, Gdk.RGBA(0, 0, 0, 0))
    transparentWindowStyleProvider = gtk.CssProvider()
    transparentWindowStyleProvider.load_from_data(b"""
        GtkWindow {
            background-color:rgba(0,0,0,0);
            background-image:none;
        }""")
    c.get_style_context().add_provider(transparentWindowStyleProvider, gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

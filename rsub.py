# encoding: utf8
import os
import socket
import sublime
import tempfile
import xml.etree.ElementTree as ET
from io import StringIO
from sublime import ENCODED_POSITION
from sublime_plugin import EventListener
from threading import Thread
try:
    from socketserver import BaseRequestHandler, ThreadingTCPServer
except ImportError:
    from SocketServer import BaseRequestHandler, ThreadingTCPServer
try:
    from ScriptingBridge import SBApplication
except ImportError:
    SBApplication = None

'''
Problems:
Double line breaks on Windows.
'''

SESSIONS = {}
server = None
syntaxes = None


class Session:

    def __init__(self, socket):
        self.env = {}
        self.file = b""
        self.file_size = 0
        self.in_file = False
        self.parse_done = False
        self.socket = socket
        self.temp_path = None

    def parse_input(self, input_line):
        if input_line.strip() == b"open" or self.parse_done:
            return

        if not self.in_file:
            input_line = input_line.decode("utf8").strip()
            if (input_line == ""):
                return
            if (input_line == "."):
                self.parse_file(b".\n")
                return
            k, v = input_line.split(":", 1)
            if k == "data":
                self.file_size = int(v)
                if len(self.env) > 1:
                    self.in_file = True
            else:
                self.env[k] = v.strip()
        else:
            self.parse_file(input_line)

    def parse_file(self, line):
        if len(self.file) >= self.file_size and line == b".\n":
            self.in_file = False
            self.parse_done = True
            sublime.set_timeout(self.on_done, 0)
        else:
            self.file += line

    def close(self):
        if not self.socket:
            return

        say("Closing connection...")
        for line in ["close", "token: " + self.env['token'], ""]:
            self.send(line + "\n")
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
            self.socket.close()
        except OSError:
            say("Can't shutdown socket, it's already gone")

        self.socket = None

    def terminate(self):
        for window in sublime.windows():
            view = window.find_open_file(self.temp_path)
            if view:
                hostname = self.env['display-name'].split(':')[0]
                view.set_status('rsub_status', "rsub: connection to %s lost" % hostname)
                title = view.name() or os.path.basename(view.file_name())
                view.set_name(u"❗" + title)

        say("Removing temporary files on disk: %s" % self.temp_dir)
        os.unlink(self.temp_path)
        os.rmdir(self.temp_dir)

    def send_save(self):
        with open(self.temp_path, 'rb') as f:
            new_file = f.read()
        for line in ["save", "token: " + self.env['token'], "data: %d" % len(new_file)]:
            self.send(line + "\n")
        self.send(new_file)
        self.send("\n")

    def send(self, data):
        if self.socket:
            try:
                if not isinstance(data, bytes):
                    data = data.encode('utf8')
                sent_bytes = self.socket.send(data)
                if sent_bytes == len(data):
                    return  # OK
            except OSError:
                pass
        say("Socket connection to the rsub client is broken!")

    def on_done(self):
        # Create a secure temporary directory, both for privacy and to allow
        # multiple files with the same basename to be edited at once without
        # overwriting each other.
        try:
            self.temp_dir = tempfile.mkdtemp(prefix='rsub-')
        except OSError as e:
            sublime.error_message('Failed to create rsub temporary directory! Error: %s' % e)
            return

        filename = os.path.basename(self.env['display-name'].split(':')[-1])
        self.temp_path = os.path.join(self.temp_dir, filename)
        try:
            temp_file = open(self.temp_path, "wb+")
            temp_file.write(self.file[:self.file_size])
            temp_file.close()
        except IOError as e:
            # Remove the file if it exists.
            if os.path.exists(self.temp_path):
                os.remove(self.temp_path)
            try:
                os.rmdir(self.temp_dir)
            except OSError:
                pass

            sublime.error_message('Failed to write to temp file! Error: %s' % str(e))

        # create new window if needed
        if len(sublime.windows()) == 0:
            sublime.run_command("new_window")

        line = self.env.get('selection', 0)

        # Open file within sublime, at provided line number if specified
        if line and line.isdigit():
            view = sublime.active_window().open_file(self.temp_path + ':' + line, ENCODED_POSITION)
        else:
            view = sublime.active_window().open_file(self.temp_path)

        # Add the file metadata to the view's settings
        # This is mostly useful to obtain the path of this file on the server
        view.settings().set('rsub', self.env)

        # Add a indicator to the status bar to indicate this is a remote file
        view.set_status('rsub_presence', u"🔴")

        # Add remote hostname to the status bar
        view.set_status('rsub_status', "rsub: " + self.env['display-name'].split(':')[0])

        # Add the session to the global list
        SESSIONS[view.id()] = self

        # Bring sublime to front
        if sublime.platform() == 'osx':
            if SBApplication:
                subl_window = SBApplication.applicationWithBundleIdentifier_("com.sublimetext.2")
                subl_window.activate()
            else:
                os.system('/usr/bin/osascript -e '
                          '\'tell app "Finder" to set frontmost of process "Sublime Text" to true\'')
        elif sublime.platform() == 'linux':
            import subprocess
            subprocess.call("wmctrl -xa 'sublime_text.sublime-text-2'", shell=True)


class ConnectionHandler(BaseRequestHandler):

    def handle(self):
        say('New connection from %s' % str(self.client_address))

        session = Session(self.request)
        self.request.send(b"Sublime Text (rsub plugin)\n")

        socket_fd = self.request.makefile('rb')
        for line in iter(socket_fd.readline, b''):
            session.parse_input(line)

        session.terminate()

        say('Connection from %s is done.' % str(self.client_address))


class TCPServer(ThreadingTCPServer):
    allow_reuse_address = True


class RSubEventListener(EventListener):

    def on_post_save(self, view):
        if view.id() in SESSIONS:
            sess = SESSIONS[view.id()]
            sess.send_save()
            say('Saved ' + sess.env['display-name'])

    def on_close(self, view):
        if view.id() in SESSIONS:
            sess = SESSIONS.pop(view.id())
            sess.close()
            say('Closed ' + sess.env['display-name'])

    def on_load(self, view):
        if view.id() in SESSIONS:
            sess = SESSIONS[view.id()]
            file_type = sess.env.get('file-type', None)
            if file_type:
                syntax = syntax_for_file_type(file_type)
                if syntax:
                    view.set_syntax_file(syntax)


def say(msg):
    print('[rsub] ' + msg)


def collect_syntax_file_types():
    """ Scan all tmLanguage resources and collect map of file type extensions.
    :returns: dict {file type : package path}
    """
    say("Collecting file type extensions of syntaxes")
    result = {}

    for path in sublime.find_resources("*.tmLanguage"):
        plist = sublime.load_resource(path)
        try:
            in_filetypes = False
            for _, elem in ET.iterparse(StringIO(plist)):
                if elem.tag == 'key' and elem.text == 'fileTypes':
                    in_filetypes = True
                elif in_filetypes and elem.tag == 'string':
                    result[elem.text] = path
                elif in_filetypes and elem.tag == 'key':
                    break
        except:
            say("Failed to parse " + path)
    return result


def syntax_for_file_type(file_type):
    """ Find syntax file for the given file type extension. Initialize global
    variable `syntaxes` if used for the first time.
    """
    global syntaxes
    if not syntaxes:
        syntaxes = collect_syntax_file_types()
    return syntaxes.get(file_type, None)


def plugin_loaded():
    """ Called by Sublime when the plugin is loaded. """
    global server

    # Load settings
    settings = sublime.load_settings("rsub.sublime-settings")
    port = settings.get("port", 52698)
    host = settings.get("host", "localhost")

    # Start server thread
    server = TCPServer((host, port), ConnectionHandler)
    Thread(target=lambda: server.serve_forever(), args=[]).start()
    say("Server running on %s:%d..." % (host, port))


def plugin_unloaded():
    """ Called by Sublime just before the plugin is unloaded. """
    global server
    say('Killing server...')
    if server:
        server.shutdown()
        server.server_close()


# call the plugin_loaded() function if running in sublime text 2
if int(sublime.version()) < 3000:
    plugin_loaded()

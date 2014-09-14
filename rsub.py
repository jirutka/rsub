# encoding: utf8
import os
import shutil
import socket
import sublime
import subprocess
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

ST_VERSION = int(int(sublime.version()) / 1000)

debug_enabled = True
sessions = {}
server = None
syntaxes = None


class Session:

    def __init__(self, socket, variables, data):
        self.socket = socket
        self.env = variables
        self.view = None

        # Create a secure temporary directory, both for privacy and to allow
        # multiple files with the same basename to be edited at once without
        # overwriting each other.
        try:
            temp_dir = tempfile.mkdtemp(prefix="rsub-")
        except OSError as e:
            sublime.error_message("Failed to create rsub temporary directory!")
            raise e

        filename = os.path.basename(variables['display-name'].split(':')[-1])
        temp_path = os.path.join(temp_dir, filename)
        try:
            with open(temp_path, 'wb+') as f:
                f.write(data)
        except IOError as e:
            sublime.error_message("Failed to write file: %s" % temp_path)
            shutil.rmtree(temp_dir, True)
            raise e

        self.temp_path = temp_path
        sublime.set_timeout(self.open_view, 0)

    def close(self):
        global sessions

        if self.socket:
            debug("Closing connection with %s", self.env['display-name'])
            for line in ["close", "token: " + self.env['token'], ""]:
                self.send(line + "\n")
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
                self.socket.close()
            except OSError:
                debug("Can't shutdown socket, it's already gone")
            self.socket = None

        # Remove itself from the global list of sessions.
        del sessions[self.view.id()]
        self.view = None

    def terminate(self):
        if self.view:
            hostname = self.env['display-name'].split(':')[0]
            self.view.set_status('rsub_status', "rsub: connection to %s lost" % hostname)
            title = self.view.name() or os.path.basename(self.view.file_name())
            self.view.set_name(u"❗" + title)

        debug("Removing temporary files on disk: %s", self.temp_path)
        os.unlink(self.temp_path)
        os.rmdir(os.path.dirname(self.temp_path))

    def send_save(self):
        info("Saving %s", self.env['display-name'])
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
        info("Socket connection to the rsub client is broken!")

    def open_view(self):
        global sessions

        # create new window if needed
        if len(sublime.windows()) == 0:
            sublime.run_command('new_window')

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
        sessions[view.id()] = self

        self.view = view

        bring_sublime_to_front()


class ConnectionHandler(BaseRequestHandler):

    def handle(self):
        """ Process incoming request from rsub client.

        This method is blocking, when finished the connection is closed.
        """
        self.session = None
        self.rfile = None

        info("New connection from %s:%d", *self.client_address)
        self.request.send(b"Sublime Text (rsub plugin)\n")

        # Create file object for reading from the socket.
        self.rfile = self.request.makefile('rb')

        for line in self.readlines():
            if line == 'open':
                self.session = self.handle_open()
            elif line == ".":
                continue
            else:
                debug("Unknown command: %s", line)

    def handle_open(self):
        """ Handle open command; read data from socket and return Session. """
        variables = {}
        data = b""

        for line in self.readlines():
            if line == "":
                break
            name, value = (s.strip() for s in line.split(":", 1))
            if name == 'data':
                data += self.rfile.read(int(value))
            else:
                variables[name] = value

        return Session(self.request, variables, data)

    def finish(self):
        info("Connection from %s:%d is done.", *self.client_address)
        self.session.terminate()

    def readlines(self):
        """ Read lines from the socket.

        :returns: generator that reads line by line from the socket, decodes
                  lines as UTF-8 and strips white spaces
        """
        return (line.decode('utf8').strip() for line in self.rfile)


class TCPServer(ThreadingTCPServer):
    allow_reuse_address = True


class RSubEventListener(EventListener):

    def session(func):
        """ Decorator for listener's methods that accepts :class:`sublime.View`.

        If there's a session for the given ``view``, then the decorated method
        is called and the session is added to the arguments list. Otherwise the
        method is not called at all.
        """
        def on_event_wrap(obj, view):
            global sessions
            session = sessions.get(view.id(), None)
            return func(obj, view, session) if session else None
        return on_event_wrap

    @session
    def on_post_save(self, view, session):
        session.send_save()

    @session
    def on_close(self, view, session):
        session.close()

    @session
    def on_load(self, view, session):
        file_type = session.env.get('file-type', None)
        if file_type:
            syntax = syntax_for_file_type(file_type)
            if syntax:
                view.set_syntax_file(syntax)


def bring_sublime_to_front():
    """ Tell Window Manager to bring Sublime Text window to front. """

    if sublime.platform() == 'osx':
        name = "Sublime Text 2" if ST_VERSION == 2 else "Sublime Text"
        os.system('/usr/bin/osascript -e '
                  '\'tell app "Finder" to set frontmost of process "%s" to true\'' % name)

    elif sublime.platform() == 'linux':
        name = "sublime-text-2" if ST_VERSION == 2 else "sublime-text"
        subprocess.call("wmctrl -xa 'sublime_text.%s'" % name, shell=True)


def collect_syntax_file_types():
    """ Scan all tmLanguage resources and collect map of file type extensions.

    :returns: dict {file type : package path}
    """
    debug("Collecting file type extensions of syntaxes.")
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
            debug("Failed to parse %s", path)
    return result


def syntax_for_file_type(file_type):
    """ Find syntax file for the given file type extension.

    It initializes global variable ``syntaxes`` if used for the first time.
    """
    global syntaxes
    if not syntaxes:
        syntaxes = collect_syntax_file_types()
    return syntaxes.get(file_type, None)


def debug(message, *args):
    global debug_enabled
    if debug_enabled:
        info(message, *args)


def info(message, *args):
    print("[rsub] " + message % args)


def plugin_loaded():
    """ Called by Sublime when the plugin is loaded. """
    global server, debug_enabled

    # Load settings
    settings = sublime.load_settings("rsub.sublime-settings")
    port = settings.get('port', 52698)
    host = settings.get('host', "localhost")
    debug_enabled = settings.get('debug', False)

    # Start server thread
    server = TCPServer((host, port), ConnectionHandler)
    Thread(target=lambda: server.serve_forever(), args=[]).start()
    info("Server running on %s:%d", host, port)


def plugin_unloaded():
    """ Called by Sublime just before the plugin is unloaded. """
    global server
    info("Killing server...")
    if server:
        server.shutdown()
        server.server_close()


if ST_VERSION < 3:
    plugin_loaded()

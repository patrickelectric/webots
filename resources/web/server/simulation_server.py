#!/usr/bin/env python

# Copyright 1996-2019 Cyberbotics Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Webots simulation server."""

from io import BytesIO
from pynvml import nvmlInit, nvmlShutdown, nvmlDeviceGetHandleByIndex, nvmlDeviceGetName, nvmlDeviceGetMemoryInfo, \
                   nvmlDeviceGetUtilizationRates
from requests import session

import errno
import json
import logging
import os
import platform
import psutil
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import tornado.ioloop
import tornado.httpserver
import tornado.web
import tornado.websocket
import traceback
import socket
import zipfile
if sys.platform == 'win32':
    import wmi


SNAPSHOT_REFRESH = 1  # make a performance measurement every second
network_sent = 0
network_received = 0


def expand_path(path):
    """Expand user and environmental variables in a string."""
    return os.path.expandvars(os.path.expanduser(path))


def mkdir_p(path):
    """Create a directory if it doesn't exit."""
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


def chmod_python_and_executable_files(directory):
    """Add executable permissions to Python and executable files."""
    if sys.platform == 'win32':
        return
    for filename in os.listdir(directory):
        fullname = os.path.join(directory, filename)
        if os.path.isdir(fullname):
            chmod_python_and_executable_files(fullname)
        if filename.endswith('.py') or not os.path.splitext(filename)[1]:
            os.chmod(fullname, 0o775)


class Snapshot:
    """This class stores instantaneous monitoring information on the machine."""

    def __init__(self):
        """Create an empty instance of MonitorSnapshot."""
        self.data = {'Timestamp': 0,
                     'Webots running': 0,
                     'Webots idle': 0,
                     'CPU load': 0,
                     'CPU memory': 0,
                     'GPU load compute': 0,
                     'GPU load memory': 0,
                     'GPU memory': 0,
                     'Swap': 0,
                     'Disk': 0,
                     'Network sent': 0,
                     'Network received': 0}

    def write(self):
        """Save a snapshot into a file."""
        if not config['monitorLogEnabled']:
            return
        global monitorFile
        file = open(monitorFile, 'a')
        file.write(str(self.data['Timestamp']) + ", ")
        file.write(str(self.data['Webots running']) + ", ")
        file.write(str(self.data['Webots idle']) + ", ")
        file.write(str(self.data['CPU load']) + ", ")
        file.write(str(self.data['CPU memory']) + ", ")
        file.write(str(self.data['GPU load compute']) + ", ")
        file.write(str(self.data['GPU load memory']) + ", ")
        file.write(str(self.data['GPU memory']) + ", ")
        file.write(str(self.data['Swap']) + ", ")
        file.write(str(self.data['Disk']) + ", ")
        file.write(str(self.data['Network sent']) + ", ")
        file.write(str(self.data['Network received']) + "\n")
        file.close()


class Client:
    """This class represents an instance of connected client."""

    def __init__(self, client_websocket=None):
        """Create an instance of client."""
        self.client_websocket = client_websocket
        self.streaming_server_port = 0
        self.webots_process = None
        self.on_webots_quit = None
        self.project_instance_path = ''
        self.app = ''
        self.world = ''
        self.idle = True

    def __del__(self):
        """Destroy an instance of client."""
        if self.client_websocket:
            self.client_websocket.close()
        self.kill_webots()
        self.cleanup_webots_instance()

    def prepare_webots_instance(self):
        """Setup a local Webots project to be run by the client."""
        appPath = config['projectsDir'] + '/' + self.app + '/'
        self.project_instance_path = config['instancesPath'] + str(id(self))
        shutil.copytree(appPath, self.project_instance_path)
        hostFile = open(self.project_instance_path + "/host.txt", 'w')
        hostFile.write(self.host)
        hostFile.close()
        if self.user1Id:
            payload = {'project': self.app, 'key': self.key,
                       'user1Id': self.user1Id, 'user1Name': self.user1Name, 'user1Authentication': self.user1Authentication,
                       'user2Id': self.user2Id, 'user2Name': self.user2Name, 'customData': self.customData}
            with session() as c:
                response = c.post(self.host + '/ajax/download-project.php', data=payload)
                if response.content.startswith(b'Error:'):
                    error = response.content.decode('utf-8')
                    if error.startswith('Error: no such directory: '):
                        return True  # Use the default directory instead
                    logging.error("Failed to download project: " + error + "(host = " + self.host + ")")
                    return False
                fp = BytesIO(response.content)
                try:
                    zfp = zipfile.ZipFile(fp, 'r')
                    zfp.extractall(self.project_instance_path)
                except zipfile.BadZipfile:
                    logging.error("Bad ZIP file:\n" + response.content.decode('utf-8'))
                    return False
                chmod_python_and_executable_files(self.project_instance_path)
        return True

    def cleanup_webots_instance(self):
        """Cleanup the local Webots project not used any more by the client."""
        if self.project_instance_path:
            shutil.rmtree(self.project_instance_path)

    def start_webots(self, on_webots_quit):
        """Start a Webots instance in a separate thread."""
        def runWebotsInThread(client):
            global config
            world = self.project_instance_path + '/worlds/' + self.world
            port = client.streaming_server_port
            command = config['webots'] + ' --batch --mode=pause --minimize '
            command += '--stream="port=' + str(port) + ';monitorActivity'
            if self.user1Authentication or not self.user1Id:  # we are running our own or an anonymous simulation
                command += ';controllerEdit'
            if 'multimediaServer' in config:
                command += ';multimediaServer=' + config['multimediaServer']
            if 'multimediaStream' in config:
                command += ';multimediaStream=' + config['multimediaStream']
            if config['ssl']:
                command += ';ssl'
            command += '" ' + world
            try:
                client.webots_process = subprocess.Popen(command.split(),
                                                         stdout=subprocess.PIPE,
                                                         stderr=subprocess.STDOUT,
                                                         bufsize=1, universal_newlines=True)
            except Exception:
                logging.error('Unable to start Webots: ' + command)
                return
            logging.info('[%d] Webots [%d] started: "%s"' % (id(client), client.webots_process.pid, command))
            while 1:
                if client.webots_process is None:
                    # client connection closed or killed
                    return
                line = client.webots_process.stdout.readline().rstrip()
                if line.startswith('open'):  # Webots world is loaded, ready to receive connections
                    break
            hostname = client.client_websocket.request.host.split(':')[0]
            if config['ssl']:
                protocol = 'wss:'
            else:
                protocol = 'ws:'
            client.client_websocket.write_message('webots:' + protocol + '//' +
                                                  hostname + ':' + str(port))
            for line in iter(client.webots_process.stdout.readline, b''):
                line = line.rstrip()
                if line == 'pause':
                    client.idle = True
                elif line == 'real-time' or line == 'step':
                    client.idle = False
                elif line == '.':
                    client.client_websocket.write_message('.')
            client.on_exit()

        if self.prepare_webots_instance():
            self.on_webots_quit = on_webots_quit
            threading.Thread(target=runWebotsInThread, args=(self,)).start()
        else:
            on_webots_quit()

    def on_exit(self):
        """Callback issued when Webots quits."""
        if self.webots_process:
            logging.warning('[%d] Webots [%d] exited' % (id(self), self.webots_process.pid))
            self.webots_process.wait()
            self.webots_process = None
        self.on_webots_quit()

    def kill_webots(self):
        """Force the termination of Webots."""
        if self.webots_process:
            logging.warning('[%d] Webots [%d] was killed' % (id(self), self.webots_process.pid))
            self.webots_process.terminate()
            self.webots_process.wait()
            self.webots_process = None
        if sys.platform == 'darwin' and self.webots_process:
            self.webots_process.kill()


class ClientWebSocketHandler(tornado.websocket.WebSocketHandler):
    """This class handles websocket connections."""

    clients = set()

    def check_origin(self, origin):
        """Allow to run the server on the same computer as the client."""
        return True

    @classmethod
    def find_client_from_websocket(cls, client_websocket):
        """Return client associated with a websocket."""
        for client in cls.clients:
            if client.client_websocket == client_websocket:
                return client
        return None

    @classmethod
    def next_available_port(cls):
        """Return a port number available for a new Webots WebSocket server."""
        port = config['port'] + 1
        while True:
            found = False
            for client in cls.clients:
                if port == client.streaming_server_port:
                    found = True
                    break
            if found:
                port += 1
            else:
                return port

    def open(self):
        """Open a new connection for an incoming client."""
        self.set_nodelay(True)
        logging.info(self.request.host)
        client = Client(client_websocket=self)
        ClientWebSocketHandler.clients.add(client)
        logging.info('[%d] New client' % (id(client),))

    def on_close(self):
        """Close connection after client leaves."""
        client = ClientWebSocketHandler.find_client_from_websocket(self)
        if client:
            logging.info('[%d] Client disconnected' % (id(client),))
            client.kill_webots()
            if client in ClientWebSocketHandler.clients:
                ClientWebSocketHandler.clients.remove(client)
                del client

    def on_message(self, message):
        """Receive message from client."""
        client = ClientWebSocketHandler.find_client_from_websocket(self)
        if client:
            data = json.loads(message)
            if 'init' in data:
                # setup client
                client.streaming_server_port = ClientWebSocketHandler.next_available_port()
                logging.info('data[init]=%s' % data['init'])
                client.host = data['init'][0]
                client.app = data['init'][1]
                client.world = data['init'][2]
                client.user1Id = data['init'][3]
                client.user1Name = data['init'][4]
                client.user1Authentication = data['init'][5]
                client.user2Id = data['init'][6]
                client.user2Name = data['init'][7]
                client.customData = data['init'][8]
                client.idle = True
                # Check that client.host is allowed
                if client.host.startswith('https://'):
                    host = client.host[8:]
                else:  # assuming 'http://'
                    host = client.host[7:]
                n = host.find(':')
                if n > 0:
                    host = host[:n]
                keyFilename = os.path.join(config['keyDir'], host)
                if (os.path.isfile(keyFilename)):
                    try:
                        keyFile = open(keyFilename, "r")
                    except IOError:
                        logging.error("Unknown host: " + host + " from " + self.request.remote_ip)
                        client.client_websocket.close()
                        return
                    client.key = keyFile.readline().rstrip(os.linesep)
                else:
                    logging.warning("No key for: " + host)
                logging.info('[%d] Setup client %s %s '
                             '(remote ip: %s, streaming_server_port: %s)'
                             % (id(client),
                                client.app,
                                client.world,
                                self.request.remote_ip,
                                client.streaming_server_port))
                self.start_client()
            elif "reset controller" in data:
                relativeFilename = '/controllers/' + data['reset controller']
                shutil.copyfile(config['projectsDir'] + '/' + client.app + relativeFilename,
                                client.project_instance_path + '/' + relativeFilename)
                self.write_message('reset controller: ' + data['reset controller'])
                logging.info('[%d] Reset file %s '
                             '(remote ip: %s, streaming_server_port: %s)'
                             % (id(client),
                                data['reset controller'],
                                self.request.remote_ip,
                                client.streaming_server_port))

    def on_webots_quit(self):
        """Cleanup websocket connection."""
        client = ClientWebSocketHandler.find_client_from_websocket(self)
        if client and client.client_websocket:
            client.client_websocket.close()

    def start_client(self):
        """Start Webots."""
        # let 10 seconds to start Webots
        self.last_supervisor_activity = None
        client = ClientWebSocketHandler.find_client_from_websocket(self)
        client.start_webots(self.on_webots_quit)


class LoadHandler(tornado.web.RequestHandler):
    """Handle load requests."""

    def get(self):
        """Return the current load of the simulation server."""
        global current_load
        self.write(str(current_load))


class MonitorHandler(tornado.web.RequestHandler):
    """Display the monitor web page."""

    global config
    global snapshots
    global nvidia

    def get(self):
        """Write the web page content."""
        global cpu_load
        global gpu_load_compute
        global gpu_load_memory
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        if nvidia:
            nvmlHandle = nvmlDeviceGetHandleByIndex(0)
            gpu = nvmlDeviceGetName(nvmlHandle)
            gpu_memory = nvmlDeviceGetMemoryInfo(nvmlHandle)
            gpu_ram = gpu_memory.total / (1024 * 1048576)
            gpu += " - " + str(gpu_ram) + "GB"
        else:
            gpu = "Not recognized"
        ram = str(int(round(float(memory.total) / (1024 * 1048576)))) + "GB"
        ram += " (swap: " + str(int(round(float(swap.total) / (1024 * 1048576)))) + "GB)"
        real_cores = psutil.cpu_count(False)
        cores_ratio = psutil.cpu_count(True) / real_cores
        cores = " (" + str(cores_ratio) + "x " + str(real_cores) + " cores)"
        if sys.platform == 'linux2':
            distribution = platform.linux_distribution()
            os_name = 'Linux ' + distribution[0] + " " + distribution[1] + " " + distribution[2]
            command = "cat /proc/cpuinfo"
            all_info = subprocess.check_output(command, shell=True).strip()
            for line in all_info.split("\n"):
                if "model name" in line:
                    cpu = re.sub(".*model name.*:", "", line, 1)
                    break
        elif sys.platform == 'win32':
            computer = wmi.WMI()
            os_info = computer.Win32_OperatingSystem()[0]
            cpu = computer.Win32_Processor()[0].Name
            os_name = os_info.Name.encode('utf-8').split('|')[0] + ", version "
            os_name += os_info.Version
        elif sys.platform == 'darwin':
            os_name = 'macOS ' + platform.mac_ver()[0]
            os.environ['PATH'] = os.environ['PATH'] + os.pathsep + '/usr/sbin'
            command = 'sysctl -n machdep.cpu.brand_string'
            cpu = subprocess.check_output(command).strip()
        else:  # unknown platform
            os_name = 'Unknown'
        self.write("<!DOCTYPE html>\n")
        self.write("<html><head><meta charset='utf-8'/><title>Webots simulation server</title>")
        self.write("<link rel='stylesheet' type='text/css' href='css/monitor.css'></head>\n")
        self.write("<body><h1>Webots simulation server: " + socket.getfqdn() + "</h1>")
        self.write("<h2>Host: " + os_name + "</h2>\n")
        self.write("<p><b>CPU load: %g%%</b><br>\n" % cpu_load)
        self.write(cpu + cores + "</p>\n")
        self.write("<p><b>GPU load compute: %g%% &mdash; load memory: %g%%</b><br>\n" %
                   (gpu_load_compute, gpu_load_memory))
        self.write(gpu + "</p>\n")
        self.write("<p><b>RAM:</b><br>" + ram + "</p>\n")
        self.write("<canvas id='graph' height='400' width='1024'></canvas>\n")
        self.write("<script src='https://www.cyberbotics.com/harry-plotter/0.9f/harry.min.js'></script>\n")
        self.write("<script>\n")
        self.write("window.onload = function() {\n")

        def appendData(label):
            global snapshots
            d = "{title:'" + label + "',values:["
            for s in snapshots:
                d += str(s.data[label]) + ','
            return d[:-1] + "]},"

        datas = ''
        datas += appendData('Webots running')
        datas += appendData('Webots idle')
        datas += appendData('CPU load')
        datas += appendData('CPU memory')
        datas += appendData('GPU load compute')
        datas += appendData('GPU load memory')
        datas += appendData('GPU memory')
        datas += appendData('Swap')
        datas += appendData('Disk')
        datas += appendData('Network sent')
        datas += appendData('Network received')

        datas = datas[:-1]  # remove the last coma
        self.write("  plotter({\n")
        self.write("    canvas: 'graph',\n")
        self.write("    datas:[ " + datas + "],\n")
        self.write("""
     labels:{
        ypos:"left",
        x:100,
        y:[50,100],
        marks:2
     },
     fill:"none",
     opacity:0.5,
     linewidth:3,
     background:"#fff",
     autoscale:"top",
     grid:{
        x:[0,100]
     },
     mouseover:{
        radius:4,
        linewidth:2,
        bullet:"#444",
        shadowbox:"1,1,0,#000",
        axis:"x"
     }
  });""")
        self.write("}\n")
        self.write("</script>\n")
        self.write("</body></html>")


def update_snapshot():
    """Compute a monitoring snapshot."""
    global current_load
    global network_sent
    global network_received
    global cpu_load
    global gpu_load_compute
    global gpu_load_memory
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk = psutil.disk_usage('/')
    n = psutil.net_io_counters()
    new_network_sent = n.bytes_sent
    new_network_received = n.bytes_recv
    network_sent_rate = float(new_network_sent - network_sent) / (SNAPSHOT_REFRESH * 1000000)  # expressed in MB/s
    network_received_rate = float(new_network_received - network_received) / (SNAPSHOT_REFRESH * 1000000)  # MB/s
    network_sent = new_network_sent
    network_received = new_network_received
    global nvidia
    if nvidia:
        nvmlHandle = nvmlDeviceGetHandleByIndex(0)
        gpu_memory = nvmlDeviceGetMemoryInfo(nvmlHandle)
        gpu_ram_usage = round(100 * float(gpu_memory.used) / float(gpu_memory.total), 1)
    else:  # not supported
        nvmlHandle = 0
        gpu_ram_usage = 0
    cpu_load = psutil.cpu_percent()
    try:
        gpu_load = nvmlDeviceGetUtilizationRates(nvmlHandle)
        gpu_load_compute = gpu_load.gpu
        gpu_load_memory = gpu_load.memory
    except:  # not supported on some hardware
        gpu_load_compute = 0
        gpu_load_memory = 0
    webots_idle = 0
    webots_running = 0
    for client in ClientWebSocketHandler.clients:
        if client.idle:
            webots_idle = webots_idle + 1
        else:
            webots_running = webots_running + 1
    snapshot = Snapshot()
    snapshot.data['Timestamp'] = int(time.time())
    snapshot.data['Webots running'] = webots_running
    snapshot.data['Webots idle'] = webots_idle
    snapshot.data['CPU load'] = cpu_load
    snapshot.data['CPU memory'] = memory.percent
    snapshot.data['GPU load compute'] = gpu_load_compute
    snapshot.data['GPU load memory'] = gpu_load_memory
    snapshot.data['GPU memory'] = gpu_ram_usage
    snapshot.data['Swap'] = swap.percent
    snapshot.data['Disk'] = disk.percent
    snapshot.data['Network sent'] = network_sent_rate
    snapshot.data['Network received'] = network_received_rate
    snapshot.write()
    current_load = 0
    for key, value in snapshot.data.items():
        if key == 'Timestamp':
            continue
        if value > current_load:
            current_load = value
    snapshots.append(snapshot)
    if len(snapshots) > 600:  # display data for the last 10 minutes
        del snapshots[0]
    tornado.ioloop.IOLoop.current().add_timeout(int(time.time()) + SNAPSHOT_REFRESH, update_snapshot)


def main():
    """Start the simulation server."""
    # the following config variables read from the config.json file
    # are described here:
    #
    # port:              local port on which the server is listening (launching webots instances).
    # sslKey:            private key for a SSL enabled server.
    # sslCertificate:    certificate for a SSL enabled server.
    # projectsDir:       directory in which projects are located.
    # keyDir:            directory where the host keys needed for validation are stored.
    # logDir:            directory where the log files are written.
    # monitorLogEnabled: specify if the monitor data have to be stored in a file.
    #
    global config
    global snapshots
    global nvidia
    global network_sent
    global network_received
    global monitorFile
    n = psutil.net_io_counters()
    network_sent = n.bytes_sent
    network_received = n.bytes_recv
    snapshots = []
    config['WEBOTS_HOME'] = os.getenv('WEBOTS_HOME', '../../..').replace('\\', '/')
    config['webots'] = config['WEBOTS_HOME']
    if sys.platform == 'darwin':
        config['webots'] += '/Contents/MacOS/webots'
    elif sys.platform == 'win32':
        config['webots'] += '/msys64/mingw64/bin/webots.exe'
    else:  # linux
        config['webots'] += '/webots'
    if 'projectsDir' not in config:
        config['projectsDir'] = config['WEBOTS_HOME'] + '/projects/samples/robotbenchmark'
    else:
        config['projectsDir'] = expand_path(config['projectsDir'])
    if 'keyDir' not in config:
        config['keyDir'] = 'key'
    else:
        config['keyDir'] = expand_path(config['keyDir'])
    if 'port' not in config:
        config['port'] = 2000
    os.environ['WEBOTS_FIREJAIL_CONTROLLERS'] = '1'
    config['instancesPath'] = tempfile.gettempdir().replace('\\', '/') + '/webots/instances/'
    # create the instances path
    if os.path.exists(config['instancesPath']):
        shutil.rmtree(config['instancesPath'])
    mkdir_p(config['instancesPath'])

    # logging system
    log_formatter = logging.Formatter('%(asctime)-15s [%(levelname)-7s]  %(message)s')
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    if 'logDir' not in config:
        config['logDir'] = 'log'
    else:
        config['logDir'] = expand_path(config['logDir'])
    simulationLogDir = os.path.join(config['logDir'], 'simulation')
    logFile = os.path.join(simulationLogDir, 'output.log')
    try:
        if not os.path.exists(simulationLogDir):
            os.makedirs(simulationLogDir)
        file_handler = logging.FileHandler(logFile)
        file_handler.setFormatter(log_formatter)
        file_handler.setLevel(logging.INFO)
        root_logger.addHandler(file_handler)
    except (OSError, IOError) as e:
        sys.exit("Log file '" + logFile + "' cannot be created: " + str(e))

    # create monitor.csv used by Snapshot if needed
    if 'monitorLogEnabled' not in config:
        config['monitorLogEnabled'] = True
    if config['monitorLogEnabled']:
        monitorFile = os.path.join(simulationLogDir, 'monitor.csv')
        try:
            if not os.path.exists(simulationLogDir):
                os.makedirs(simulationLogDir)
            file = open(monitorFile, 'w')
            file.write("Timestamp, Webots running, Webots idle, CPU load, CPU memory, "
                       "GPU load compute, GPU load memory, GPU memory, Swap, Disk, Network sent, Network received\n")
            file.close()
        except (OSError, IOError) as e:
            logging.error("Log file '" + monitorFile + "' cannot be created: " + str(e))

    # startup janus server if needed
    if 'multimediaServer' in config:
        subprocess.Popen(["/opt/janus/bin/janus"])

    # startup the server
    logging.info("Running simulation server on port %d" % config['port'])

    handlers = []
    handlers.append((r'/monitor', MonitorHandler))
    handlers.append((r'/client', ClientWebSocketHandler))
    handlers.append((r'/load', LoadHandler))
    handlers.append((r'/(.*)', tornado.web.StaticFileHandler,
                    {'path': config['WEBOTS_HOME'] + '/resources/web/server/www',
                     'default_filename': 'index.html'}))
    application = tornado.web.Application(handlers)
    if 'sslCertificate' in config and 'sslKey' in config:
        config['ssl'] = True
        ssl_certificate = os.path.abspath(expand_path(config['sslCertificate']))
        ssl_key = os.path.abspath(expand_path(config['sslKey']))
        ssl_options = {"certfile": ssl_certificate, "keyfile": ssl_key}
        http_server = tornado.httpserver.HTTPServer(application, ssl_options=ssl_options)
    else:
        config['ssl'] = False
        http_server = tornado.httpserver.HTTPServer(application)
    http_server.listen(config['port'])
    message = "Simulation server running on port %d (" % config['port']
    if not config['ssl']:
        message += 'no '
    message += 'SSL)'
    print(message)
    sys.stdout.flush()
    try:
        nvmlInit()
        nvidia = True
    except:
        nvidia = False
    update_snapshot()
    try:
        tornado.ioloop.IOLoop.current().start()
    except Exception:
        logging.info(traceback.format_exc())
        for client in ClientWebSocketHandler.clients:
            del client
    if nvidia:
        nvmlShutdown()


if sys.platform == 'linux2':
    # kill all the existing instances of Webots to avoid conflicts with web socket port
    os.system("killall -q webots-bin")

# specify the display to ensure Webots can be executed even if this script is started remotely from a ssh session
os.environ["DISPLAY"] = ":0"
# ensure we are in the script directory
os.chdir(os.path.dirname(os.path.realpath(__file__)))
argc = len(sys.argv)
if argc == 1:
    config_json = 'config/simulation/default.json'
elif argc == 2:
    config_json = sys.argv[1]
else:
    sys.exit('Too many arguments.')
with open(config_json) as config_file:
    config = json.load(config_file)
if __name__ == '__main__':
    main()

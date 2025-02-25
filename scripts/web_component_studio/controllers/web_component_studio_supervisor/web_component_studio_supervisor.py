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

"""Create a web component scene foreach robot of the robots.json file."""

from controller import Supervisor
import json
import glob
import os
import shutil
from lxml import etree


def _cmp(a, b):
    return (a > b) - (a < b)


def _compareDevice(d1, d2):
    priortyDeviceTypes = ['RotationalMotor', 'LinearMotor', 'LED']  # Device types appearing first.
    for priortyDeviceType in priortyDeviceTypes:
        if d1['type'] == priortyDeviceType and d2['type'] == priortyDeviceType:
            return _cmp(d1['name'].lower(), d2['name'].lower())
        elif d1['type'] == priortyDeviceType:
            return -1
        elif d2['type'] == priortyDeviceType:
            return 1
    return _cmp(d1['name'].lower(), d2['name'].lower())


userGuidePath = os.path.join(os.getenv('WEBOTS_HOME'), 'docs', 'guide')

supervisor = Supervisor()
timeStep = int(supervisor.getBasicTimeStep())

robot = supervisor.getFromDef('ROBOT')
robotName = robot.getField('name').getSFString()

# Get target paths.
scenePath = os.path.join(userGuidePath, 'scenes', robotName)
targetHTMLFile = os.path.join(scenePath, robotName + '.html')
targetAnimationFile = os.path.join(scenePath, robotName + '.json')
targetMetaFile = os.path.join(scenePath, robotName + '.meta.json')
targetX3DFile = os.path.join(scenePath, robotName + '.x3d')

# Store the scene.
if os.path.exists(scenePath):
    shutil.rmtree(scenePath)
if not os.path.exists(scenePath):
    os.makedirs(scenePath)
supervisor.animationStartRecording(targetHTMLFile)
supervisor.animationStopRecording()
supervisor.step(timeStep)
supervisor.step(timeStep)

# Remove useless files.
os.remove(targetHTMLFile)
os.remove(targetAnimationFile)
for fl in glob.glob(os.path.join(scenePath, 'textures', 'cubic', 'noon_cloudy_mountains*.jpg')):
    os.remove(fl)

# Simplified JSON file.
# - keep only the interested robot.
assert os.path.exists(targetMetaFile), 'The meta file does not exists. ' \
    'Please run Webots with the "--enable-x3d-meta-file-export" argument.'
with open(targetMetaFile) as f:
    robotsMetaData = json.load(f)
    robotData = None
    for _robotData in robotsMetaData:
        if _robotData['name'] == robotName:
            robotData = _robotData
            break
    assert robotData, 'Failed to simplified the JSON supervisor.'
    # - sort the device list per interesting category type.
    robotData['devices'] = sorted(robotData['devices'], cmp=_compareDevice)
    # - rewrite the json file.
    with open(targetMetaFile, 'w') as f:
        json.dump(robotData, f, indent=2)
        f.write('\n')

    # Corrections on the XML file.
    tree = etree.parse(targetX3DFile)
    # Global texture paths.
    background = tree.xpath('//Background')
    background[0].attrib['rightUrl'] = background[0].attrib['rightUrl'].replace('textures/cubic/', '../background/')
    background[0].attrib['leftUrl'] = background[0].attrib['leftUrl'].replace('textures/cubic/', '../background/')
    background[0].attrib['topUrl'] = background[0].attrib['topUrl'].replace('textures/cubic/', '../background/')
    background[0].attrib['bottomUrl'] = background[0].attrib['bottomUrl'].replace('textures/cubic/', '../background/')
    background[0].attrib['frontUrl'] = background[0].attrib['frontUrl'].replace('textures/cubic/', '../background/')
    background[0].attrib['backUrl'] = background[0].attrib['backUrl'].replace('textures/cubic/', '../background/')
    tree.write(targetX3DFile, pretty_print=True, xml_declaration=True, encoding="utf-8")

    supervisor.step(timeStep)

    supervisor.simulationQuit(0)

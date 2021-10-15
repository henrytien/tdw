from os import urandom
import base64
import math
import json
from pathlib import Path
from pkg_resources import resource_filename
from csv import DictReader
import io
from typing import Dict, Optional, Union, List, Tuple
import numpy as np
import scipy.signal as sg
from scipy.ndimage import gaussian_filter1d
from pydub import AudioSegment
from tdw.output_data import OutputData, Rigidbodies, Collision, EnvironmentCollision, StaticRobot, SegmentationColors, \
    StaticRigidbodies, RobotJointVelocities
from tdw.collision_data.collision_obj_obj import CollisionObjObj
from tdw.collision_data.collision_obj_env import CollisionObjEnv
from tdw.physics_audio.audio_material import AudioMaterial
from tdw.physics_audio.object_audio_static import ObjectAudioStatic
from tdw.physics_audio.modes import Modes
from tdw.physics_audio.base64_sound import Base64Sound
from tdw.physics_audio.collision_audio_info import CollisionAudioInfo
from tdw.physics_audio.collision_audio_type import CollisionAudioType
from tdw.physics_audio.collision_audio_event import CollisionAudioEvent
from tdw.object_data.rigidbody import Rigidbody
from tdw.audio_constants import SAMPLE_RATE, CHANNELS
from tdw.add_ons.collision_manager import AddOn


class PyImpact(AddOn):
    """
    Generate impact sounds from physics data.

    Sounds are synthesized as described in: [Traer,Cusimano and McDermott, A PERCEPTUALLY INSPIRED GENERATIVE MODEL OF RIGID-BODY CONTACT SOUNDS, Digital Audio Effects, (DAFx), 2019](http://dafx2019.bcu.ac.uk/papers/DAFx2019_paper_57.pdf)

    For a general guide on impact sounds in TDW, read [this](../misc_frontend/impact_sounds.md).

    For example usage, see: `tdw/Python/example_controllers/impact_sounds.py`
    """

    """:class_var
    The width of a scrape sample.
    """
    SCRAPE_SAMPLE_WIDTH: int = 2
    """:class_var
    The scrape surface.
    """
    SCRAPE_SURFACE: np.array = np.load(resource_filename(__name__, f"py_impact/scrape_surface.npy"))
    SCRAPE_SURFACE = np.append(SCRAPE_SURFACE, SCRAPE_SURFACE)
    """:class_var
    50ms of silence. Used for scrapes.
    """
    SILENCE_50MS: AudioSegment = AudioSegment.silent(duration=50, frame_rate=SAMPLE_RATE)
    """:class_var
    The maximum velocity allowed for a scrape.
    """
    SCRAPE_MAX_VELOCITY: float = 5.0
    """:class_var
    Meters per pixel on the scrape surface.
    """
    SCRAPE_M_PER_PIXEL: float = 1394.068 * 10 ** -9
    """:class_var
    The target decibels for scrapes.
    """
    SCRAPE_TARGET_DBFS: float = -20.0
    """:class_var
    The default amp value for objects.
    """
    DEFAULT_AMP: float = 0.2
    """:class_var
    The default [material](../physics_audio/audio_material.md) for objects.
    """
    DEFAULT_MATERIAL: AudioMaterial = AudioMaterial.plastic_hard
    """:class_var
    The default resonance value for objects.
    """
    DEFAULT_RESONANCE: float = 0.45
    """:class_var
    The default audio size "bucket" for objects.
    """
    DEFAULT_SIZE: int = 1
    """:class_var
    The assumed bounciness value for robot joints.
    """
    ROBOT_JOINT_BOUNCINESS: float = 0.6
    """:class_var
    The [material](../physics_audio/audio_material.md) used for robot joints.
    """
    ROBOT_JOINT_MATERIAL: AudioMaterial = AudioMaterial.metal
    """:class_var
    The amp value for the floor.
    """
    FLOOR_AMP: float = 0.5
    """:class_var
    The size "bucket" for the floor.
    """
    FLOOR_SIZE: int = 4
    """:class_var
    The mass of the floor.
    """
    FLOOR_MASS: int = 100

    def __init__(self, initial_amp: float = 0.5, prevent_distortion: bool = True, logging: bool = False,
                 static_audio_data_overrides: Dict[str, ObjectAudioStatic] = None,
                 resonance_audio: bool = False, floor: AudioMaterial = AudioMaterial.wood_medium):
        """
        :param initial_amp: The initial amplitude, i.e. the "master volume". Must be > 0 and < 1.
        :param prevent_distortion: If True, clamp amp values to <= 0.99
        :param logging: If True, log mode properties for all colliding objects, as json.
        """

        super().__init__()

        assert 0 < initial_amp < 1, f"initial_amp is {initial_amp} (must be > 0 and < 1)."

        self.initial_amp = initial_amp
        self.prevent_distortion = prevent_distortion
        self.logging = logging

        # The collision info per set of objects.
        self.object_modes: Dict[int, Dict[int, CollisionAudioInfo]] = {}
        self.resonance_audio: bool = resonance_audio
        self.floor: AudioMaterial = floor

        # Cache the material data. This is use to reset the material modes.
        self.material_data: Dict[str, dict] = {}
        material_list = ["ceramic", "wood_hard", "wood_medium", "wood_soft", "metal", "glass", "paper", "cardboard",
                         "leather", "fabric", "plastic_hard", "plastic_soft_foam", "rubber", "stone"]
        for mat in material_list:
            for i in range(6):
                # Load the JSON data.
                mat_name = mat + "_" + str(i)
                path = mat_name + "_mm"
                data = json.loads(Path(resource_filename(__name__, f"py_impact/material_data/{path}.json")).read_text())
                self.material_data.update({mat_name: data})

        # Create empty dictionary for log.
        self.mode_properties_log = dict()

        self.static_audio_data_overrides: Dict[str, ObjectAudioStatic] = dict()
        if static_audio_data_overrides is not None:
            self.static_audio_data_overrides = static_audio_data_overrides

        self._collision_events: Dict[int, CollisionAudioEvent] = dict()

        self._cached_audio_info: bool = False
        self.static_audio_data: Dict[int, ObjectAudioStatic] = dict()

        # Summed scrape masters. Key = primary ID, secondary ID.
        self._scrape_summed_masters: Dict[Tuple[int, int], AudioSegment] = dict()
        # Keeping a track of previous scrape indices.
        self._scrape_previous_index: int = 0
        # Starting velocity magnitude of scraping object; use in calculating changing band-pass filter.
        self._scrape_start_velocities: Dict[Tuple[int, int], float] = dict()
        # Initialize the scraping event counter.
        self._scrape_events_count: Dict[Tuple[int, int], int] = dict()

    def get_initialization_commands(self) -> List[dict]:
        return [{"$type": "send_rigidbodies",
                 "frequency": "always"},
                {"$type": "send_robot_joint_velocities",
                 "frequency": "always"},
                {"$type": "send_collisions",
                 "enter": True,
                 "exit": True,
                 "stay": True,
                 "collision_types": ["obj", "env"]},
                {"$type": "send_static_robots"},
                {"$type": "send_segmentation_colors"},
                {"$type": "send_static_rigidbodies"}]

    def on_send(self, resp: List[bytes]) -> None:
        """
        This is called after commands are sent to the build and a response is received.

        Use this function to send commands to the build on the next frame, given the `resp` response.
        Any commands in the `self.commands` list will be sent on the next frame.

        :param resp: The response from the build.
        """

        # Cache static audio info.
        if not self._cached_audio_info:
            self._cached_audio_info = True
            self._cache_static_data(resp=resp)
        # Get collision events.
        self._get_collision_types(resp=resp)
        for object_id in self._collision_events:
            command = None
            # Generate an impact sound.
            if self._collision_events[object_id].collision_type == CollisionAudioType.impact:
                # Generate an environment sound.
                if self._collision_events[object_id].secondary_id is None:
                    audio = self.static_audio_data[object_id]
                    command = self.get_impact_sound_command(velocity=self._collision_events[object_id].velocity,
                                                            contact_points=self._collision_events[object_id].collision.points,
                                                            contact_normals=self._collision_events[object_id].collision.normals,
                                                            primary_id=object_id,
                                                            primary_amp=audio.amp,
                                                            primary_material=audio.material.name + "_" + str(audio.size),
                                                            primary_mass=audio.mass,
                                                            secondary_id=None,
                                                            secondary_amp=PyImpact.FLOOR_AMP,
                                                            secondary_material=self._get_floor_material_name(),
                                                            secondary_mass=PyImpact.FLOOR_MASS,
                                                            resonance=audio.resonance)
                # Generate an object sound.
                else:
                    target_audio = self.static_audio_data[self._collision_events[object_id].primary_id]
                    other_audio = self.static_audio_data[self._collision_events[object_id].secondary_id]
                    command = self.get_impact_sound_command(velocity=self._collision_events[object_id].velocity,
                                                            contact_points=self._collision_events[object_id].collision.points,
                                                            contact_normals=self._collision_events[object_id].collision.normals,
                                                            primary_id=target_audio.object_id,
                                                            primary_amp=target_audio.amp,
                                                            primary_material=target_audio.material.name + "_" + str(
                                                                target_audio.size),
                                                            primary_mass=target_audio.mass,
                                                            secondary_id=other_audio.object_id,
                                                            secondary_amp=other_audio.amp,
                                                            secondary_material=other_audio.material.name + "_" + str(
                                                                other_audio.size),
                                                            secondary_mass=other_audio.mass,
                                                            resonance=target_audio.resonance)
            # Generate a scrape sound.
            elif self._collision_events[object_id].collision_type == CollisionAudioType.scrape:
                # Generate an environment sound.
                if self._collision_events[object_id].secondary_id is None:
                    audio = self.static_audio_data[object_id]
                    command = self.get_scrape_sound_command(velocity=self._collision_events[object_id].velocity,
                                                            contact_points=self._collision_events[object_id].collision.points,
                                                            contact_normals=self._collision_events[object_id].collision.normals,
                                                            primary_id=object_id,
                                                            primary_amp=audio.amp,
                                                            primary_material=audio.material.name + "_" + str(audio.size),
                                                            primary_mass=audio.mass,
                                                            secondary_id=None,
                                                            secondary_amp=PyImpact.FLOOR_AMP,
                                                            secondary_material=self._get_floor_material_name(),
                                                            secondary_mass=PyImpact.FLOOR_MASS,
                                                            resonance=audio.resonance)
                # Generate an object sound.
                else:
                    target_audio = self.static_audio_data[self._collision_events[object_id].primary_id]
                    other_audio = self.static_audio_data[self._collision_events[object_id].secondary_id]
                    command = self.get_scrape_sound_command(velocity=self._collision_events[object_id].velocity,
                                                            contact_points=self._collision_events[object_id].collision.points,
                                                            contact_normals=self._collision_events[object_id].collision.normals,
                                                            primary_id=target_audio.object_id,
                                                            primary_amp=target_audio.amp,
                                                            primary_material=target_audio.material.name + "_" + str(
                                                                target_audio.size),
                                                            primary_mass=target_audio.mass,
                                                            secondary_id=other_audio.object_id,
                                                            secondary_amp=other_audio.amp,
                                                            secondary_material=other_audio.material.name + "_" + str(
                                                                other_audio.size),
                                                            secondary_mass=other_audio.mass,
                                                            resonance=target_audio.resonance)
            # Append impact sound commands.
            if command is not None:
                self.commands.append(command)

    def _get_floor_material_name(self) -> str:
        """
        :return: The name of the floor material.
        """

        # We probably need dedicated wall and floor materials, or maybe they are in size category #6?
        # Setting to "4" for now, for general debugging purposes.
        return f"{self.floor.name}_{PyImpact.FLOOR_SIZE}"

    def _get_collision_types(self, resp: List[bytes]) -> None:
        """
        Get all collision types on this frame. Update previous area data.

        :param resp: The response from the build.
        """

        # Collision events per object on this frame. We'll only care about the most significant one.
        collision_events_per_object: Dict[int, List[CollisionAudioEvent]] = dict()
        # Get the previous areas.
        previous_areas: Dict[int, float] = {k: v.area for k, v in self._collision_events.items()}
        # Clear the collision events.
        self._collision_events.clear()
        rigidbody_data: Dict[int, Rigidbody] = dict()
        for i in range(len(resp) - 1):
            r_id = OutputData.get_data_type_id(resp[i])
            # Get rigidbody data.
            if r_id == "rigi":
                rigidbodies = Rigidbodies(resp[i])
                for j in range(rigidbodies.get_num()):
                    rigidbody_data[rigidbodies.get_id(j)] = Rigidbody(velocity=np.array(rigidbodies.get_velocity(j)),
                                                                      angular_velocity=np.array(
                                                                          rigidbodies.get_angular_velocity(j)),
                                                                      sleeping=rigidbodies.get_sleeping(j))
            # Get robot joint velocity data.
            elif r_id == "rojv":
                robot_joint_velocities = RobotJointVelocities(resp[i])
                for j in range(robot_joint_velocities.get_num_joints()):
                    rigidbody_data[robot_joint_velocities.get_joint_id(j)] = Rigidbody(velocity=robot_joint_velocities.get_joint_velocity(j),
                                                                                       angular_velocity=robot_joint_velocities.get_joint_angular_velocity(j),
                                                                                       sleeping=robot_joint_velocities.get_joint_sleeping(j))
        # Get collision data.
        for i in range(len(resp) - 1):
            r_id = OutputData.get_data_type_id(resp[i])
            # Parse a collision.
            if r_id == "coll":
                collision = Collision(resp[i])
                collider_id = collision.get_collider_id()
                collidee_id = collision.get_collidee_id()
                event = CollisionAudioEvent(collision=CollisionObjObj(collision),
                                            object_0_static=self.static_audio_data[collider_id],
                                            object_0_dynamic=rigidbody_data[collider_id],
                                            object_1_static=self.static_audio_data[collidee_id],
                                            object_1_dynamic=rigidbody_data[collidee_id],
                                            previous_areas=previous_areas)
                if event.primary_id not in collision_events_per_object:
                    collision_events_per_object[event.primary_id] = list()
                collision_events_per_object[event.primary_id].append(event)
            # Parse an environment collision.
            elif r_id == "enco":
                collision = EnvironmentCollision(resp[i])
                collider_id = collision.get_object_id()
                event = CollisionAudioEvent(collision=CollisionObjEnv(collision),
                                            object_0_static=self.static_audio_data[collider_id],
                                            object_0_dynamic=rigidbody_data[collider_id],
                                            previous_areas=previous_areas)
                if event.primary_id not in collision_events_per_object:
                    collision_events_per_object[event.primary_id] = list()
                collision_events_per_object[event.primary_id].append(event)
        # Get the significant collision events per object.
        for primary_id in collision_events_per_object:
            events: List[CollisionAudioEvent] = [e for e in collision_events_per_object[primary_id] if e.magnitude > 0 
                                                 and e.collision_type != CollisionAudioType.none]
            if len(events) > 0:
                event: CollisionAudioEvent = max(events, key=lambda x: x.magnitude)
                self._collision_events[event.primary_id] = event

    def get_log(self) -> dict:
        """
        :return: The mode properties log.
        """

        return self.mode_properties_log

    def _get_object_modes(self, material: Union[str, AudioMaterial]) -> Modes:
        """
        :param material: The audio material.

        :return: The audio modes.
        """
        data = self.material_data[material] if isinstance(material, str) else self.material_data[material.name]
        # Load the mode properties.
        f = -1
        p = -1
        t = -1
        for jm in range(0, 10):
            jf = 0
            while jf < 20:
                jf = data["cf"][jm] + np.random.normal(0, data["cf"][jm] / 10)
            jp = data["op"][jm] + np.random.normal(0, 10)
            jt = 0
            while jt < 0.001:
                jt = data["rt"][jm] + np.random.normal(0, data["rt"][jm] / 10)
            if jm == 0:
                f = jf
                p = jp
                t = jt * 1e3
            else:
                f = np.append(f, jf)
                p = np.append(p, jp)
                t = np.append(t, jt * 1e3)
        return Modes(f, p, t)

    def get_sound(self, velocity: np.array, contact_normals: List[np.array],
                  primary_id: int, primary_material: str, primary_amp: float, primary_mass: float,
                  secondary_id: Optional[int], secondary_material: str, secondary_amp: float, secondary_mass: float,
                  resonance: float) -> Optional[Base64Sound]:
        """
        Produce sound of two colliding objects as a byte array.

        :param primary_id: The object ID for the primary (target) object.
        :param primary_material: The material label for the primary (target) object.
        :param secondary_id: The object ID for the secondary (other) object.
        :param secondary_material: The material label for the secondary (other) object.
        :param primary_amp: Sound amplitude of primary (target) object.
        :param secondary_amp: Sound amplitude of the secondary (other) object.
        :param resonance: The resonances of the objects.
        :param velocity: The velocity.
        :param contact_normals: The collision contact normals.
        :param primary_mass: The mass of the primary (target) object.
        :param secondary_mass: The mass of the secondary (target) object.

        :return Sound data as a Base64Sound object.
        """

        # The sound amplitude of object 2 relative to that of object 1.
        amp2re1 = secondary_amp / primary_amp

        # Set the object modes.
        if secondary_id not in self.object_modes:
            self.object_modes.update({secondary_id: {}})
        if primary_id not in self.object_modes[secondary_id]:
            self.object_modes[secondary_id].update({primary_id: CollisionAudioInfo(self._get_object_modes(secondary_material),
                                                                                   self._get_object_modes(
                                                                                       primary_material),
                                                                                   amp=primary_amp * self.initial_amp)})
        # Unpack useful parameters.
        speed = np.square(velocity)
        speed = np.sum(speed)
        speed = math.sqrt(speed)
        nvel = velocity / np.linalg.norm(velocity)
        nspd = []
        for jc in range(len(contact_normals)):
            tmp = np.asarray(contact_normals[jc])
            tmp = tmp / np.linalg.norm(tmp)
            tmp = np.arccos(np.clip(np.dot(tmp, nvel), -1.0, 1.0))
            # Scale the speed by the angle (i.e. we want speed Normal to the surface).
            tmp = speed * np.cos(tmp)
            nspd.append(tmp)
        normal_speed = np.mean(nspd)
        mass = np.min([primary_mass, secondary_mass])

        # Re-scale the amplitude.
        if self.object_modes[secondary_id][primary_id].count == 0:
            # Sample the modes.
            sound, modes_1, modes_2 = self.make_impact_audio(amp2re1, mass,
                                                             mat1=primary_material,
                                                             mat2=secondary_material,
                                                             id1=primary_id,
                                                             id2=secondary_id,
                                                             resonance=resonance)
            # Save collision info - we will need for later collisions.
            amp = self.object_modes[secondary_id][primary_id].amp
            self.object_modes[secondary_id][primary_id].init_speed = normal_speed
            self.object_modes[secondary_id][primary_id].obj1_modes = modes_1
            self.object_modes[secondary_id][primary_id].obj2_modes = modes_2

        else:
            amp = self.object_modes[secondary_id][primary_id].amp * normal_speed / self.object_modes[secondary_id][primary_id].init_speed
            # Adjust modes here so that two successive impacts are not identical.
            modes_1 = self.object_modes[secondary_id][primary_id].obj1_modes
            modes_2 = self.object_modes[secondary_id][primary_id].obj2_modes
            modes_1.powers = modes_1.powers + np.random.normal(0, 2, len(modes_1.powers))
            modes_2.powers = modes_2.powers + np.random.normal(0, 2, len(modes_2.powers))
            sound = PyImpact.synth_impact_modes(modes_1, modes_2, mass, resonance)
            self.object_modes[secondary_id][primary_id].obj1_modes = modes_1
            self.object_modes[secondary_id][primary_id].obj2_modes = modes_2

        if self.logging:
            mode_props = dict()
            self.log_modes(self.object_modes[secondary_id][primary_id].count, mode_props, primary_id, secondary_id,
                           modes_1, modes_2, amp, primary_material, secondary_material)
            
        # On rare occasions, it is possible for PyImpact to fail to generate a sound.
        if sound is None:
            return None

        # Count the collisions.
        self.object_modes[secondary_id][primary_id].count_collisions()

        # Prevent distortion by clamping the amp.
        if self.prevent_distortion and np.abs(amp) > 0.99:
            amp = 0.99

        sound = amp * sound / np.max(np.abs(sound))
        return Base64Sound(sound)

    def get_impact_sound_command(self, velocity: np.array, contact_points: List[np.array],
                                 contact_normals: List[np.array], primary_id: int,
                                 primary_material: str, primary_amp: float, primary_mass: float,
                                 secondary_id: Optional[int], secondary_material: str, secondary_amp: float,
                                 secondary_mass: float, resonance: float) -> Optional[dict]:
        """
        Create an impact sound, and return a valid command to play audio data in TDW.
        "target" should usually be the smaller object, which will play the sound.
        "other" should be the larger (stationary) object.

        :param primary_id: The object ID for the primary (target) object.
        :param primary_material: The material label for the primary (target) object.
        :param secondary_id: The object ID for the secondary (other) object.
        :param secondary_material: The material label for the secondary (other) object.
        :param primary_amp: Sound amplitude of primary (target) object.
        :param secondary_amp: Sound amplitude of the secondary (other) object.
        :param resonance: The resonances of the objects.
        :param velocity: The velocity.
        :param contact_points: The collision contact points.
        :param contact_normals: The collision contact normals.
        :param primary_mass: The mass of the primary (target) object.
        :param secondary_mass: The mass of the secondary (target) object.

        :return A `play_audio_data` or `play_point_source_data` command that can be sent to the build via `Controller.communicate()`.
        """

        impact_audio = self.get_sound(velocity=velocity, contact_normals=contact_normals, primary_id=primary_id,
                                      primary_material=primary_material, primary_amp=primary_amp,
                                      primary_mass=primary_mass, secondary_id=secondary_id, secondary_material=secondary_material,
                                      secondary_amp=secondary_amp, secondary_mass=secondary_mass, resonance=resonance)
        if impact_audio is not None:
            point = np.mean(contact_points, axis=0)
            return {"$type": "play_audio_data" if not self.resonance_audio else "play_point_source_data",
                    "id": PyImpact._get_unique_id(),
                    "position": {"x": float(point[0]), "y": float(point[1]), "z": float(point[2])},
                    "num_frames": impact_audio.length,
                    "num_channels": CHANNELS,
                    "frame_rate": SAMPLE_RATE,
                    "wav_data": impact_audio.wav_str,
                    "y_pos_offset": 0.1}
        # If PyImpact failed to generate a sound (which is rare!), fail silently here.
        else:
            return None

    def make_impact_audio(self, amp2re1: float, mass: float, id1: int, id2: int, resonance: float, mat1: str = 'cardboard', mat2: str = 'cardboard') -> (np.array, Modes, Modes):
        """
        Generate an impact sound.

        :param mat1: The material label for one of the colliding objects.
        :param mat2: The material label for the other object.
        :param amp2re1: The sound amplitude of object 2 relative to that of object 1.
        :param mass: The mass of the smaller of the two colliding objects.
        :param id1: The ID for the one of the colliding objects.
        :param id2: The ID for the other object.
        :param resonance: The resonance of the objects.

        :return The sound, and the object modes.
        """

        # Unpack material names.
        for jmat in AudioMaterial:
            if mat1 == jmat:
                tmp1 = jmat
                mat1 = tmp1.name
            if mat2 == jmat:
                tmp2 = jmat
                mat2 = tmp2.name
        # Sample modes of object1.
        modes_1 = self.object_modes[id2][id1].obj1_modes
        modes_2 = self.object_modes[id2][id1].obj2_modes
        # Scale the two sounds as specified.
        modes_2.decay_times = modes_2.decay_times + 20 * np.log10(amp2re1)
        snth = PyImpact.synth_impact_modes(modes_1, modes_2, mass, resonance)
        return snth, modes_1, modes_2

    def get_impulse_response(self, velocity: np.array, contact_normals: List[np.array], primary_id: int,
                             primary_material: str, primary_amp: float, primary_mass: float,
                             secondary_id: int, secondary_material: str, secondary_amp: float, secondary_mass: float,
                             resonance: float) -> np.array:
        """
        Generate an impulse response from the modes for two specified objects.

        :param primary_id: The object ID for the primary (target) object.
        :param primary_material: The material label for the primary (target) object.
        :param secondary_id: The object ID for the secondary (other) object.
        :param secondary_material: The material label for the secondary (other) object.
        :param primary_amp: Sound amplitude of primary (target) object.
        :param secondary_amp: Sound amplitude of the secondary (other) object.
        :param resonance: The resonances of the objects.
        :param velocity: The velocity.
        :param contact_normals: The collision contact normals.
        :param primary_mass: The mass of the primary (target) object.
        :param secondary_mass: The mass of the secondary (target) object.

        :return The impulse response.
        """

        self.get_sound(velocity=velocity, contact_normals=contact_normals, primary_id=primary_id,
                       primary_material=primary_material, primary_amp=primary_amp,
                       primary_mass=primary_mass, secondary_id=secondary_id, secondary_material=secondary_material,
                       secondary_amp=secondary_amp, secondary_mass=secondary_mass, resonance=resonance)

        modes_1 = self.object_modes[secondary_id][primary_id].obj1_modes
        modes_2 = self.object_modes[secondary_id][primary_id].obj2_modes
        h1 = modes_1.sum_modes(resonance=resonance)
        h2 = modes_2.sum_modes(resonance=resonance)
        h = Modes.mode_add(h1, h2)
        return h, min(modes_1.frequencies)

    def get_scrape_sound_command(self, velocity: np.array, contact_points: np.array,
                                 contact_normals: List[np.array], primary_id: int,
                                 primary_material: str, primary_amp: float, primary_mass: float,
                                 secondary_id: Optional[int], secondary_material: str, secondary_amp: float,
                                 secondary_mass: float, resonance: float) -> Optional[dict]:
        """
        :param primary_id: The object ID for the primary (target) object.
        :param primary_material: The material label for the primary (target) object.
        :param secondary_id: The object ID for the secondary (other) object.
        :param secondary_material: The material label for the secondary (other) object.
        :param primary_amp: Sound amplitude of primary (target) object.
        :param secondary_amp: Sound amplitude of the secondary (other) object.
        :param resonance: The resonances of the objects.
        :param velocity: The velocity.
        :param contact_points: The collision contact points.
        :param contact_normals: The collision contact normals.
        :param primary_mass: The mass of the primary (target) object.
        :param secondary_mass: The mass of the secondary (target) object.

        :return A command to play a scrape sound.
        """

        sound = self.get_scrape_sound(velocity=velocity,
                                      contact_normals=contact_normals,
                                      primary_id=primary_id,
                                      primary_material=primary_material,
                                      primary_amp=primary_amp,
                                      primary_mass=primary_mass,
                                      secondary_id=secondary_id,
                                      secondary_material=secondary_material,
                                      secondary_amp=secondary_amp,
                                      secondary_mass=secondary_mass,
                                      resonance=resonance)
        if sound is None:
            return None
        else:
            point = np.mean(contact_points, axis=0)
            return {"$type": "play_audio_data" if not self.resonance_audio else "play_point_source_data",
                    "id": PyImpact._get_unique_id(),
                    "position": {"x": float(point[0]), "y": float(point[1]), "z": float(point[2])},
                    "num_frames": sound.length,
                    "num_channels": CHANNELS,
                    "frame_rate": SAMPLE_RATE,
                    "wav_data": sound.wav_str,
                    "y_pos_offset": 0}

    def get_scrape_sound(self, velocity: np.array, contact_normals: List[np.array], primary_id: int,
                         primary_material: str, primary_amp: float, primary_mass: float,
                         secondary_id: int, secondary_material: str, secondary_amp: float, secondary_mass: float,
                         resonance: float) -> Optional[Base64Sound]:
        """
        Create a scrape sound, and return a valid command to play audio data in TDW.
        "target" should usually be the smaller object, which will play the sound.
        "other" should be the larger (stationary) object.

        :param primary_id: The object ID for the primary (target) object.
        :param primary_material: The material label for the primary (target) object.
        :param secondary_id: The object ID for the secondary (other) object.
        :param secondary_material: The material label for the secondary (other) object.
        :param primary_amp: Sound amplitude of primary (target) object.
        :param secondary_amp: Sound amplitude of the secondary (other) object.
        :param resonance: The resonances of the objects.
        :param velocity: The velocity.
        :param contact_normals: The collision contact normals.
        :param primary_mass: The mass of the primary (target) object.
        :param secondary_mass: The mass of the secondary (target) object.

        :return A [`Base64Sound`](../physics_audio/base64_sound.md) object or None if no sound.
        """

        scrape_key: Tuple[int, int] = (primary_id, secondary_id)

        # Initialize scrape variables; if this is an in=process scrape, these will be replaced bu te stored values.
        summed_master = AudioSegment.silent(duration=0, frame_rate=SAMPLE_RATE)
        scrape_event_count = 0

        # Is this a new scrape?
        if scrape_key in self._scrape_summed_masters:
            summed_master = self._scrape_summed_masters[scrape_key]
            scrape_event_count = self._scrape_events_count[scrape_key]
        else:
            # No -- add initialized values to dictionaries.
            self._scrape_summed_masters[scrape_key] = summed_master
            self._scrape_events_count[scrape_key] = scrape_event_count

        # Get magnitude of velocity of the scraping object.
        mag = min(np.linalg.norm(velocity), PyImpact.SCRAPE_MAX_VELOCITY)

        # Cache the starting velocity.
        if scrape_event_count == 0:
            self._scrape_start_velocities[scrape_key] = mag

        # Map magnitude to gain level -- decrease in velocity = rise in negative dB, i.e. decrease in gain.
        db = np.interp(mag ** 2, [0, PyImpact.SCRAPE_MAX_VELOCITY ** 2], [-80, -12])

        # Get impulse response of the colliding objects. Amp values would normally come from objects.csv.
        # We also get the lowest-frequency IR mode, which we use to set the high-pass filter cutoff below.
        scraping_ir, min_mode_freq = self.get_impulse_response(velocity=velocity,
                                                               contact_normals=contact_normals,
                                                               primary_id=primary_id,
                                                               primary_material=primary_material,
                                                               primary_amp=primary_amp,
                                                               primary_mass=primary_mass,
                                                               secondary_id=secondary_id,
                                                               secondary_material=secondary_material,
                                                               secondary_amp=secondary_amp,
                                                               secondary_mass=secondary_mass,
                                                               resonance=resonance)

        #   Load the surface texture as a 1D vector
        #   Create surface texture of desired length
        #   Calculate first and second derivatives by first principles
        #   Apply non-linearity on the second derivative
        #   Apply a variable Gaussian average
        #   Calculate the horizontal and vertical forces
        #   Convolve the force with the impulse response
        dsdx = (PyImpact.SCRAPE_SURFACE[1:] - PyImpact.SCRAPE_SURFACE[0:-1]) / PyImpact.SCRAPE_M_PER_PIXEL
        d2sdx2 = (dsdx[1:] - dsdx[0:-1]) / PyImpact.SCRAPE_M_PER_PIXEL

        dist = mag / 1000
        num_pts = np.floor(dist / PyImpact.SCRAPE_M_PER_PIXEL)
        num_pts = int(num_pts)
        if num_pts == 0:
            num_pts = 1
        # No scrape.
        if num_pts == 1:
            self._end_scrape(scrape_key)
            return None

        # interpolate the surface slopes and curvatures based on the velocity magnitude
        final_ind = self._scrape_previous_index + num_pts

        if final_ind > len(PyImpact.SCRAPE_SURFACE) - 100:
            self._scrape_previous_index = 0
            final_ind = num_pts

        vect1 = np.linspace(0, 1, num_pts)
        vect2 = np.linspace(0, 1, 4010)

        slope_int = np.interp(vect2, vect1, dsdx[self._scrape_previous_index:final_ind])
        curve_int = np.interp(vect2, vect1, d2sdx2[self._scrape_previous_index:final_ind])

        self._scrape_previous_index = final_ind

        curve_int_tan = np.tanh(curve_int / 1000)

        d2_section = gaussian_filter1d(curve_int_tan, 10)

        vert_force = d2_section
        hor_force = slope_int

        t_force = vert_force / max(np.abs(vert_force)) + 0.2 * hor_force[:len(vert_force)]

        noise_seg1 = AudioSegment(t_force.tobytes(),
                                  frame_rate=SAMPLE_RATE,
                                  sample_width=PyImpact.SCRAPE_SAMPLE_WIDTH,
                                  channels=CHANNELS)
        # Normalize gain.
        noise_seg1.apply_gain(PyImpact.SCRAPE_TARGET_DBFS)

        # Fade head and tail.
        noise_seg_fade = noise_seg1.fade_in(4).fade_out(4)
        # Convolve the band-pass filtered sound with the impulse response.
        conv = sg.fftconvolve(scraping_ir, noise_seg_fade.get_array_of_samples())

        # Again, we need this as an AudioSegment for overlaying with the previous frame's segment.
        # Convert to 16-bit integers for Unity, normalizing to make sure to minimize loss of precision from truncating floating values.
        normalized_noise_ints_conv = PyImpact._normalize_16bit_int(conv)
        noise_seg_conv = AudioSegment(normalized_noise_ints_conv.tobytes(),
                                      frame_rate=SAMPLE_RATE,
                                      sample_width=PyImpact.SCRAPE_SAMPLE_WIDTH,
                                      channels=CHANNELS)

        # Gain-adjust the convolved segment using db value computed earlier.
        noise_seg_conv = noise_seg_conv.apply_gain(db)
        if scrape_event_count == 0:
            # First time through -- append 50 ms of silence to the end of the current segment and make that "master".
            summed_master = noise_seg_conv + PyImpact.SILENCE_50MS
        elif scrape_event_count == 1:
            # Second time through -- append 50 ms silence to start of current segment and overlay onto "master".
            summed_master = summed_master.overlay(PyImpact.SILENCE_50MS + noise_seg_conv)
        else:
            # Pad the end of master with 50 ms of silence, the start of the current segment with (n * 50ms) of silence, and overlay.
            padded_current = (PyImpact.SILENCE_50MS * scrape_event_count) + noise_seg_conv
            summed_master = summed_master + PyImpact.SILENCE_50MS
            summed_master = summed_master.overlay(padded_current)

        # Extract 100ms "chunk" of sound to send over to Unity.
        start_idx = 100 * scrape_event_count
        temp = summed_master[-(len(summed_master) - start_idx):]
        unity_chunk = temp[:100]
        # Update stored summed waveform.
        self._scrape_summed_masters[scrape_key] = summed_master

        # Update scrape event count.
        scrape_event_count += 1
        self._scrape_events_count[scrape_key] = scrape_event_count
        # Scrape data is handled differently than impact data, so we'll create a dummy object first.
        sound = Base64Sound(np.array([0]))
        # Set the audio data.
        sound.wav_str = base64.b64encode(unity_chunk.raw_data).decode()
        sound.length = len(unity_chunk.raw_data)
        sound.bytes = unity_chunk.raw_data
        return sound

    @staticmethod
    def synth_impact_modes(modes1: Modes, modes2: Modes, mass: float, resonance: float) -> np.array:
        """
        Generate an impact sound from specified modes for two objects, and the mass of the smaller object.

        :param modes1: Modes of object 1. A numpy array with: column1=mode frequencies (Hz); column2=mode onset powers in dB; column3=mode RT60s in milliseconds;
        :param modes2: Modes of object 2. Formatted as modes1/modes2.
        :param mass: the mass of the smaller of the two colliding objects.
        :param resonance: The resonance of the objects.

        :return The impact sound.
        """

        h1 = modes1.sum_modes(resonance=resonance)
        h2 = modes2.sum_modes(resonance=resonance)
        h = Modes.mode_add(h1, h2)
        if len(h) == 0:
            return None
        # Convolve with force, with contact time scaled by the object mass.
        max_t = 0.001 * mass
        # A contact time over 2ms is unphysically long.
        max_t = np.min([max_t, 2e-3])
        n_pts = int(np.ceil(max_t * 44100))
        tt = np.linspace(0, np.pi, n_pts)
        frc = np.sin(tt)
        x = sg.fftconvolve(h, frc)
        x = x / abs(np.max(x))
        return x

    @staticmethod
    def get_static_audio_data(csv_file: Union[str, Path] = "") -> Dict[str, ObjectAudioStatic]:
        """
        Returns ObjectInfo values.
        As of right now, only a few objects in the TDW model libraries are included. More will be added in time.

        :param csv_file: The path to the .csv file containing the object info. By default, it will load `tdw/py_impact/objects.csv`. If you want to make your own spreadsheet, use this file as a reference.

        :return: A list of default ObjectInfo. Key = the name of the model. Value = object info.
        """

        objects: Dict[str, ObjectAudioStatic] = {}
        # Load the objects.csv metadata file.
        if isinstance(csv_file, str):
            # Load the default file.
            if csv_file == "":
                csv_file = str(Path(resource_filename(__name__, f"py_impact/objects.csv")).resolve())
            else:
                csv_file = str(Path(csv_file).resolve())
        else:
            csv_file = str(csv_file.resolve())

        # Parse the .csv file.
        with io.open(csv_file, newline='', encoding='utf-8-sig') as f:
            reader = DictReader(f)
            for row in reader:
                o = ObjectAudioStatic(name=row["name"], amp=float(row["amp"]), mass=float(row["mass"]),
                                      material=AudioMaterial[row["material"]], bounciness=float(row["bounciness"]),
                                      resonance=float(row["resonance"]), size=int(row["size"]), object_id=0)
                objects.update({o.name: o})

        return objects

    def reset(self, initial_amp: float = 0.5) -> None:
        """
        Reset PyImpact. This is somewhat faster than creating a new PyImpact object per trial.

        :param initial_amp: The initial amplitude, i.e. the "master volume". Must be > 0 and < 1.
        """

        assert 0 < initial_amp < 1, f"initial_amp is {initial_amp} (must be > 0 and < 1)."
        self._cached_audio_info = False
        self.initialized = False
        self.static_audio_data.clear()
        # Clear the object data.
        self.object_modes.clear()
        # Clear collision data.
        self._collision_events.clear()
        # Clear scrape data.
        self._scrape_summed_masters.clear()
        self._scrape_previous_index = 0
        self._scrape_start_velocities.clear()
        self._scrape_events_count.clear()

    def log_modes(self, count: int, mode_props: dict, id1: int, id2: int, modes_1: Modes, modes_2: Modes, amp: float, mat1: str, mat2: str):
        """
        Log mode properties info for a single collision event.

        :param count: Mode count for this material-material collision.
        :param mode_props: Dictionary to log to.
        :param id1: ID of the "other" object.
        :param id2: ID of the "target" object.
        :param modes_1: Modes of the "other" object.
        :param modes_2: Modes of the "target" object.
        :param amp: Adjusted amplitude value of collision.
        :param mat1: Material of the "other" object.
        :param mat2: Material of the "target" object.
        """

        mode_props["modes_count"] = count
        mode_props["other_id"] = id1
        mode_props["target_id"] = id2
        mode_props["amp"] = amp
        mode_props["other_material"] = mat1
        mode_props["target_material"] = mat2
        mode_props["modes_1.frequencies"] = modes_1.frequencies.tolist()
        mode_props["modes_1.powers"] = modes_1.powers.tolist()
        mode_props["modes_1.decay_times"] = modes_1.decay_times.tolist()
        mode_props["modes_2.frequencies"] = modes_2.frequencies.tolist()
        mode_props["modes_2.powers"] = modes_2.powers.tolist()
        mode_props["modes_2.decay_times"] = modes_2.decay_times.tolist()
        self.mode_properties_log[str(id1) + "_" + str(id2) + "__" + str(count)] = mode_props

    def _cache_static_data(self, resp: List[bytes]) -> None:
        """
        Cache static data.

        :param resp: The response from the build.
        """

        # Load the default object info.
        default_static_audio_data = PyImpact.get_static_audio_data()
        categories: Dict[int, str] = dict()
        names: Dict[int, str] = dict()
        robot_joints: Dict[int, dict] = dict()
        object_masses: Dict[int, float] = dict()
        object_bouncinesses: Dict[int, float] = dict()
        for i in range(len(resp) - 1):
            r_id = OutputData.get_data_type_id(resp[i])
            if r_id == "segm":
                segm = SegmentationColors(resp[i])
                for j in range(segm.get_num()):
                    object_id = segm.get_object_id(j)
                    names[object_id] = segm.get_object_name(j).lower()
                    categories[object_id] = segm.get_object_category(j)
            elif r_id == "srob":
                srob = StaticRobot(resp[i])
                for j in range(srob.get_num_joints()):
                    joint_id = srob.get_joint_id(j)
                    robot_joints[joint_id] = {"name": srob.get_joint_name(j),
                                              "mass": srob.get_joint_mass(j)}
            elif r_id == "srig":
                srig = StaticRigidbodies(resp[i])
                for j in range(srig.get_num()):
                    object_masses[srig.get_id(j)] = srig.get_mass(j)
                    object_bouncinesses[srig.get_id(j)] = srig.get_bounciness(j)
        need_to_derive: List[int] = list()
        for object_id in names:
            name = names[object_id]
            # Use override data.
            if name in self.static_audio_data_overrides:
                self.static_audio_data[object_id] = self.static_audio_data_overrides[name]
                self.static_audio_data[object_id].mass = object_masses[object_id]
                self.static_audio_data[object_id].object_id = object_id
            # Use default audio data.
            elif name in default_static_audio_data:
                self.static_audio_data[object_id] = default_static_audio_data[name]
                self.static_audio_data[object_id].mass = object_masses[object_id]
                self.static_audio_data[object_id].object_id = object_id
            else:
                need_to_derive.append(object_id)
        current_values = self.static_audio_data.values()
        derived_data: Dict[int, ObjectAudioStatic] = dict()
        for object_id in need_to_derive:
            # Fallback option: comparable objects in the same category.
            objects_in_same_category = [o for o in categories if categories[o] == categories[object_id]]
            if len(objects_in_same_category) > 0:
                amps: List[float] = [a.amp for a in current_values]
                materials: List[AudioMaterial] = [a.material for a in current_values]
                resonances: List[float] = [a.resonance for a in current_values]
                sizes: List[int] = [a.size for a in current_values]
            # Fallback option: Find objects with similar volume.
            else:
                amps: List[float] = list()
                materials: List[AudioMaterial] = list()
                resonances: List[float] = list()
                sizes: List[int] = list()
                for m_id in object_masses:
                    if m_id == object_id or m_id not in self.static_audio_data:
                        continue
                    if np.abs(object_masses[m_id] / object_masses[object_id]) < 1.5:
                        amps.append(self.static_audio_data[m_id].amp)
                        materials.append(self.static_audio_data[m_id].material)
                        resonances.append(self.static_audio_data[m_id].resonance)
                        sizes.append(self.static_audio_data[m_id].size)
            # Fallback option: Use default values.
            if len(amps) == 0:
                amp: float = PyImpact.DEFAULT_AMP
                material: AudioMaterial = PyImpact.DEFAULT_MATERIAL
                resonance: float = PyImpact.DEFAULT_RESONANCE
                size: int = PyImpact.DEFAULT_SIZE
            # Get averages or maximums of each value.
            else:
                amp: float = round(sum(amps) / len(amps), 3)
                material: AudioMaterial = max(set(materials), key=materials.count)
                resonance: float = round(sum(resonances) / len(resonances), 3)
                size: int = int(sum(sizes) / len(sizes))
            derived_data[object_id] = ObjectAudioStatic(name=names[object_id],
                                                        mass=object_masses[object_id],
                                                        material=material,
                                                        bounciness=object_bouncinesses[object_id],
                                                        resonance=resonance,
                                                        size=size,
                                                        amp=amp,
                                                        object_id=object_id)
        # Add the derived data.
        for object_id in derived_data:
            self.static_audio_data[object_id] = derived_data[object_id]
        # Add robot joints.
        for joint_id in robot_joints:
            self.static_audio_data[joint_id] = ObjectAudioStatic(name=robot_joints[joint_id]["name"],
                                                                 mass=robot_joints[joint_id]["mass"],
                                                                 material=PyImpact.ROBOT_JOINT_MATERIAL,
                                                                 bounciness=PyImpact.ROBOT_JOINT_BOUNCINESS,
                                                                 resonance=PyImpact.DEFAULT_RESONANCE,
                                                                 size=PyImpact.DEFAULT_SIZE,
                                                                 amp=PyImpact.DEFAULT_AMP,
                                                                 object_id=joint_id)

    @staticmethod
    def _normalize_16bit_int(arr: np.array) -> np.array:
        """
        Convert numpy float array to normalized 16-bit integers.

        :param arr: Numpy float data to convert.

        :return: The converted numpy array.
        """

        normalized_floats = PyImpact._normalize_floats(arr)

        return (normalized_floats * 32767).astype(np.int16)

    @staticmethod
    def _normalize_floats(arr: np.array) -> np.array:
        """
        Normalize numpy array of float audio data.

        :param arr: Numpy float data to normalize.

        :return The normalized array.
        """

        if np.all(arr == 0):
            return arr
        else:
            return arr / np.abs(arr).max()

    def _end_scrape(self, scrape_key: Tuple[int, int]) -> None:
        """
        Clean up after a given scrape event has ended.
        """

        if scrape_key in self._scrape_events_count:
            del self._scrape_events_count[scrape_key]
        if scrape_key in self._scrape_summed_masters:
            del self._scrape_summed_masters[scrape_key]
        if scrape_key in self._scrape_start_velocities:
            del self._scrape_start_velocities[scrape_key]

    @staticmethod
    def _get_unique_id() -> int:
        """
        Generate a unique integer. Useful when creating objects.

        :return The new unique ID.
        """

        return int.from_bytes(urandom(3), byteorder='big')
from os import urandom
import numpy as np
import math
import json
import scipy.signal as sg
from typing import Dict, Optional, Tuple, Union, List
from tdw.output_data import OutputData, Rigidbodies, Collision, EnvironmentCollision, Robot
from pathlib import Path
from pkg_resources import resource_filename
from csv import DictReader
import io
from tdw.physics_audio.audio_material import AudioMaterial
from tdw.physics_audio.object_audio import ObjectAudio
from tdw.physics_audio.modes import Modes
from tdw.physics_audio.base64_sound import Base64Sound
from tdw.physics_audio.collision_audio_info import CollisionAudioInfo
from tdw.physics_audio.collision_audio_type import CollisionAudioType
from tdw.int_pair import IntPair


class PyImpact:
    """
    Generate impact sounds from physics data.

    Sounds are synthesized as described in: [Traer,Cusimano and McDermott, A PERCEPTUALLY INSPIRED GENERATIVE MODEL OF RIGID-BODY CONTACT SOUNDS, Digital Audio Effects, (DAFx), 2019](http://dafx2019.bcu.ac.uk/papers/DAFx2019_paper_57.pdf)

    For a general guide on impact sounds in TDW, read [this](../misc_frontend/impact_sounds.md).

    For example usage, see: `tdw/Python/example_controllers/impact_sounds.py`
    """

    def __init__(self, initial_amp: float = 0.5, prevent_distortion: bool = True, logging: bool = False):
        """
        :param initial_amp: The initial amplitude, i.e. the "master volume". Must be > 0 and < 1.
        :param prevent_distortion: If True, clamp amp values to <= 0.99
        :param logging: If True, log mode properties for all colliding objects, as json.
        """

        assert 0 < initial_amp < 1, f"initial_amp is {initial_amp} (must be > 0 and < 1)."

        self.initial_amp = initial_amp
        self.prevent_distortion = prevent_distortion
        self.logging = logging

        # The collision info per set of objects.
        self.object_modes: Dict[int, Dict[int, CollisionAudioInfo]] = {}

        # Cache the material data. This is use to reset the material modes.
        self.material_data: Dict[str, dict] = {}
        material_list = ["ceramic", "wood_hard", "wood_medium", "wood_soft", "metal", "glass", "paper", "cardboard", "leather", "fabric", "plastic_hard", "plastic_soft_foam", "rubber", "stone"]
        for mat in material_list:
            for i in range(6):
                # Load the JSON data.
                mat_name = mat + "_" + str(i)
                path = mat_name + "_mm"
                data = json.loads(Path(resource_filename(__name__, f"py_impact/material_data/{path}.json")).read_text())
                self.material_data.update({mat_name: data})

        # Create empty dictionary for log.
        self.mode_properties_log = dict()

        # Get the default object info. See: `set_default_audio_info()`
        self.object_info: Dict[str, ObjectAudio] = dict()
        # A dictionary of objects in the scene. See: `set_default_audio_info()`
        self.object_names: Dict[int, str] = dict()
        # A dummy ID for the environment. See: `set_default_audio_info()`
        self.env_id: int = -1

    @staticmethod
    def get_collision_types(resp: List[bytes]) -> Dict[CollisionAudioType, List[Union[Collision, EnvironmentCollision]]]:
        obj_enters: Dict[IntPair, List[Collision]] = dict()
        obj_stays: Dict[IntPair, List[Collision]] = dict()
        obj_exits: Dict[IntPair, List[Collision]] = dict()
        env_enters: Dict[int, List[EnvironmentCollision]] = dict()
        env_stays: Dict[int, List[EnvironmentCollision]] = dict()
        env_exits: Dict[int, List[EnvironmentCollision]] = dict()
        angular_velocities: Dict[int, np.array] = dict()
        velocities: Dict[int, np.array] = dict()
        for i in range(len(resp) - 1):
            r_id = OutputData.get_data_type_id(resp[i])
            if r_id == "rigi":
                rigidbodies = Rigidbodies(resp[i])
                for j in range(rigidbodies.get_num()):
                    angular_velocities[rigidbodies.get_id(j)] = np.array(rigidbodies.get_angular_velocity(j))
                    velocities[rigidbodies.get_id(j)] = np.array(rigidbodies.get_velocity(j))
            elif r_id == "coll":
                collision = Collision(resp[i])
                ids = IntPair(collision.get_collider_id(), collision.get_collidee_id())
                # Sort the events.
                if collision.get_state() == "enter":
                    if ids not in obj_enters:
                        obj_enters[ids] = list()
                    obj_enters[ids].append(collision)
                elif collision.get_state() == "exit":
                    if ids not in obj_exits:
                        obj_exits[ids] = list()
                    obj_exits[ids].append(collision)
                elif collision.get_state() == "stay":
                    if ids not in obj_stays:
                        obj_stays[ids] = list()
                    obj_stays[ids].append(collision)
            elif r_id == "enco":
                environment_collision = EnvironmentCollision(resp[i])
                object_id = environment_collision.get_object_id()
                # Sort the events.
                if environment_collision.get_state() == "enter":
                    if object_id not in env_enters:
                        env_enters[object_id] = list()
                    env_enters[object_id].append(environment_collision)
                elif environment_collision.get_state() == "exit":
                    if object_id not in env_exits:
                        env_exits[object_id] = list()
                    env_exits[object_id].append(environment_collision)
                elif environment_collision.get_state() == "stay":
                    if object_id not in env_stays:
                        env_stays[object_id] = list()
                    env_stays[object_id].append(environment_collision)

        # Remove any enter events that are also stay or exit events.
        obj_enters = {k: v for k, v in obj_enters.items() if k not in obj_exits and k not in obj_stays}
        env_enters = {k: v for k, v in env_enters.items() if k not in env_exits and k not in env_stays}

        # Remove any exit events that are also stay events.
        obj_exits = {k: v for k, v in obj_exits.items() if k not in obj_stays}
        env_exits = {k: v for k, v in env_exits.items() if k not in env_stays}

        collisions: Dict[CollisionAudioType, List[Union[Collision, EnvironmentCollision]]] = {CollisionAudioType.impact: [],
                                                                                              CollisionAudioType.scrape: [],
                                                                                              CollisionAudioType.roll: [],
                                                                                              CollisionAudioType.none: []}
        # Impacts are enter events.
        for k in obj_enters:
            collisions[CollisionAudioType.impact].extend(obj_enters[k])
        for k in env_enters:
            collisions[CollisionAudioType.impact].extend(env_enters[k])
        # Rolls are stay events with high angular velocity. Scrapes are stay events with low angular velocity.
        for k in obj_stays:
            if np.linalg.norm(angular_velocities[k.int1]) > 0.1 or np.linalg.norm(angular_velocities[k.int2]) > 0.1:
                collisions[CollisionAudioType.roll].extend(obj_stays[k])
            else:
                collisions[CollisionAudioType.scrape].extend(obj_stays[k])
        for k in env_stays:
            if np.linalg.norm(angular_velocities[k]) > 0.1:
                collisions[CollisionAudioType.roll].extend(env_stays[k])
            else:
                collisions[CollisionAudioType.scrape].extend(env_stays[k])
        # None events are exits.
        for k in obj_exits:
            collisions[CollisionAudioType.none].extend(obj_exits[k])
        for k in env_exits:
            collisions[CollisionAudioType.none].extend(env_exits[k])
        return collisions

    def set_default_audio_info(self, object_names: Dict[int, str]) -> None:
        """
        Set the default audio info for each object in the scene.

        :param object_names: A dictionary of objects in the scene. Key = The object ID. Value = The name of the model.
        """

        self.object_names = object_names
        self.object_info = PyImpact.get_object_info()
        self.env_id = int.from_bytes(urandom(3), byteorder='big')

    def get_audio_commands(self, resp: List[bytes], floor: AudioMaterial, wall: AudioMaterial, resonance_audio: bool = False) -> List[dict]:
        """
        Given the response from the build, get audio commands.

        Required:

        - You've already called `set_default_audio_info()` and supplied it with the IDs and names of each object in the scene:
        - You've requested collision and rigidbody data per-frame (`send_collisions` and `send_rigidbodies`)
        - If there are robots in the scene, you've already requests robot data per-frame (`send_robots`)

        :param resp: The response from the build.
        :param floor: The floor material as an `AudioMaterial` enum value.
        :param wall: The wall material as an `AudioMaterial` enum value.
        :param resonance_audio: If True, this function assumes you're using Resonance Audio.

        :return: A list of commands that will play audio generated by the collisions on this frame. This can be empty.
        """

        # Get all robot joints in the scene.
        joint_ids = []
        for i in range(len(resp) - 1):
            r_id = OutputData.get_data_type_id(resp[i])
            if r_id == "robo":
                robo = Robot(resp[i])
                for j in range(robo.get_num_joints()):
                    joint_ids.append(robo.get_joint_id(j))

        commands = []
        collisions, env_collisions, rigidbodies = self.get_collisions(resp=resp)
        # Get the mass and speed of each object.
        speeds: Dict[int, float] = dict()
        for i in range(rigidbodies.get_num()):
            object_id = rigidbodies.get_id(i)
            speeds[object_id] = np.linalg.norm(rigidbodies.get_velocity(i))
        # Get all stays and exits.
        stays_and_exits: List[int] = list()
        for c in collisions:
            if c.get_state() == "stay" or c.get_state() == "exit":
                stays_and_exits.extend([c.get_collider_id(), c.get_collidee_id()])
        colliders: List[int] = list()
        # Play sounds from collisions.
        for collision in collisions:
            # Ignore invalid collisions.
            if collision.get_state() != "enter" or not self.is_valid_collision(collision=collision):
                continue
            collider_id = collision.get_collider_id()
            collidee_id = collision.get_collidee_id()
            # Ignore collisions if there is a "stay" event, because this can create a droning effect.
            if collider_id in stays_and_exits or collidee_id in stays_and_exits or\
                    collider_id in colliders or collidee_id in colliders:
                continue
            colliders.append(collider_id)
            colliders.append(collidee_id)
            # Skip objects that for some reason aren't in the cached data.
            if collider_id not in self.object_names or collidee_id not in self.object_names:
                continue
            # The target object is the one with less mass.
            if self.object_info[self.object_names[collider_id]].mass < \
                    self.object_info[self.object_names[collidee_id]].mass:
                target = collider_id
                other = collidee_id
            else:
                target = collidee_id
                other = collider_id

            target_audio = self.object_info[self.object_names[target]]
            other_audio = self.object_info[self.object_names[other]]
            commands.append(self.get_impact_sound_command(collision=collision,
                                                          rigidbodies=rigidbodies,
                                                          target_id=target,
                                                          target_amp=target_audio.amp,
                                                          target_mat=target_audio.material.name + "_" + str(target_audio.size),
                                                          other_id=other,
                                                          other_amp=other_audio.amp,
                                                          other_mat=other_audio.material.name + "_" + str(other_audio.size),
                                                          resonance=target_audio.resonance,
                                                          play_audio_data=not resonance_audio,
                                                          robot_joints=joint_ids))
        # Play sounds from collisions with the environment.
        stays_and_exits.extend([c.get_object_id() for c in env_collisions if
                                c.get_state() == "stay" or c.get_state() == "exit"])
        for collision in env_collisions:
            target = collision.get_object_id()
            # Ignore collisions that aren't enter, not a floor, that aren't in the cached data, or that are too slow.
            # When objects are initially spawned they collide with the environment at very slow speeds,
            # resulting in a "click" sound that we don't actually want.
            if collision.get_state() != "enter" or not collision.get_floor() or target not in self.object_names or \
                    speeds[target] < 0.01 or target in colliders or target in stays_and_exits:
                continue
            colliders.append(target)
            audio = self.object_info[self.object_names[target]]
            commands.append(self.get_impact_sound_command(collision=collision,
                                                          rigidbodies=rigidbodies,
                                                          target_id=target,
                                                          target_amp=audio.amp,
                                                          target_mat=audio.material.name + "_" + str(audio.size),
                                                          other_id=self.env_id,
                                                          other_amp=0.5,
                                                          # We probably need dedicated wall and floor materials, or maybe they are in size category #6?
                                                          # Setting to "4" for now, for general debugging purposes
                                                          other_mat=floor.name + "_4",
                                                          resonance=audio.resonance,
                                                          play_audio_data=not resonance_audio))
        return commands

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

    def get_sound(self, collision: Union[Collision, EnvironmentCollision], rigidbodies: Rigidbodies, id1: int, mat1: str, id2: int, mat2: str, other_amp: float, target_amp: float, resonance: float) -> Optional[Base64Sound]:
        """
        Produce sound of two colliding objects as a byte array.

        :param collision: TDW `Collision` or `EnvironmentCollision` output data.
        :param rigidbodies: TDW `Rigidbodies` output data.
        :param id1: The object ID for one of the colliding objects.
        :param mat1: The material label for one of the colliding objects.
        :param id2: The object ID for the other object.
        :param mat2: The material label for the other object.
        :param other_amp: Sound amplitude of object 2.
        :param target_amp: Sound amplitude of object 1.
        :param resonance: The resonances of the objects.

        :return Sound data as a Base64Sound object.
        """

        # The sound amplitude of object 2 relative to that of object 1.
        amp2re1 = other_amp / target_amp

        # Set the object modes.
        if id2 not in self.object_modes:
            self.object_modes.update({id2: {}})
        if id1 not in self.object_modes[id2]:
            self.object_modes[id2].update({id1: CollisionAudioInfo(self._get_object_modes(mat2),
                                                                   self._get_object_modes(mat1),
                                                                   amp=target_amp * self.initial_amp)})
        obj_col = isinstance(collision, Collision)

        # Unpack useful parameters.
        # Compute normal velocity at impact.
        vel = 0
        if obj_col:
            vel = collision.get_relative_velocity()
        else:
            for i in range(rigidbodies.get_num()):
                if rigidbodies.get_id(i) == id2:
                    vel = rigidbodies.get_velocity(i)
                    # If the y coordinate of the velocity is negative, it implies a scrape or roll along the floor.
                    if vel[1] < 0:
                        return None
                    break
        vel = np.asarray(vel)
        speed = np.square(vel)
        speed = np.sum(speed)
        speed = math.sqrt(speed)
        nvel = vel / np.linalg.norm(vel)
        num_contacts = collision.get_num_contacts()
        nspd = []
        for jc in range(0, num_contacts):
            tmp = np.asarray(collision.get_contact_normal(jc))
            tmp = tmp / np.linalg.norm(tmp)
            tmp = np.arccos(np.clip(np.dot(tmp, nvel), -1.0, 1.0))
            # Scale the speed by the angle (i.e. we want speed Normal to the surface).
            tmp = speed * np.cos(tmp)
            nspd.append(tmp)
        normal_speed = np.mean(nspd)
        # Get indices of objects in collisions.
        id1_index = None
        id2_index = None

        for i in range(rigidbodies.get_num()):
            if rigidbodies.get_id(i) == id1:
                id1_index = i
            if rigidbodies.get_id(i) == id2:
                id2_index = i

        # Use default values for environment collisions.
        if not obj_col:
            m1 = 100
            m2 = rigidbodies.get_mass(id2_index)
        # Use the Rigidbody masses.
        elif id1_index is not None and id2_index is not None:
            m1 = rigidbodies.get_mass(id1_index)
            m2 = rigidbodies.get_mass(id2_index)
        # Fallback: Try to use default mass values if the ID's aren't in the Rigidbody data.
        elif id1 in self.object_names and id2 in self.object_names and self.object_names[id1] in self.object_info and \
                self.object_names[id2] in self.object_info:
            m1 = self.object_info[self.object_names[id1]].mass
            m2 = self.object_info[self.object_names[id2]].mass
        # Failed to generate a sound.
        else:
            return None
        mass = np.min([m1, m2])

        # Re-scale the amplitude.
        if self.object_modes[id2][id1].count == 0:
            # Sample the modes.
            sound, modes_1, modes_2 = self.make_impact_audio(amp2re1, mass, mat1=mat1, mat2=mat2, id1=id1, id2=id2, resonance=resonance)
            # Save collision info - we will need for later collisions.
            amp = self.object_modes[id2][id1].amp
            self.object_modes[id2][id1].init_speed = normal_speed
            self.object_modes[id2][id1].obj1_modes = modes_1
            self.object_modes[id2][id1].obj2_modes = modes_2

        else:
            amp = self.object_modes[id2][id1].amp * normal_speed / self.object_modes[id2][id1].init_speed
            # Adjust modes here so that two successive impacts are not identical.
            modes_1 = self.object_modes[id2][id1].obj1_modes
            modes_2 = self.object_modes[id2][id1].obj2_modes
            modes_1.powers = modes_1.powers + np.random.normal(0, 2, len(modes_1.powers))
            modes_2.powers = modes_2.powers + np.random.normal(0, 2, len(modes_2.powers))
            sound = PyImpact.synth_impact_modes(modes_1, modes_2, mass, resonance)
            self.object_modes[id2][id1].obj1_modes = modes_1
            self.object_modes[id2][id1].obj2_modes = modes_2

        if self.logging:
            mode_props = dict()
            self.log_modes(self.object_modes[id2][id1].count, mode_props, id1, id2, modes_1, modes_2, amp, str(mat1), str(mat2))
            
        # On rare occasions, it is possible for PyImpact to fail to generate a sound.
        if sound is None:
            return None

        # Count the collisions.
        self.object_modes[id2][id1].count_collisions()

        # Prevent distortion by clamping the amp.
        if self.prevent_distortion and np.abs(amp) > 0.99:
            amp = 0.99

        sound = amp * sound / np.max(np.abs(sound))
        return Base64Sound(sound)

    def get_impact_sound_command(self, collision: Union[Collision, EnvironmentCollision], rigidbodies: Rigidbodies, target_id: int, target_mat: str, target_amp: float, other_id: int, other_mat: str, other_amp: float, resonance: float, play_audio_data: bool = True, robot_joints: List[int] = None) -> dict:
        """
        Create an impact sound, and return a valid command to play audio data in TDW.
        "target" should usually be the smaller object, which will play the sound.
        "other" should be the larger (stationary) object.

        :param collision: TDW `Collision` or `EnvironmentCollision` output data.
        :param target_amp: The target's amp value.
        :param target_mat: The target's audio material.
        :param other_amp: The other object's amp value.
        :param other_id: The other object's ID.
        :param other_mat: The other object's audio material.
        :param rigidbodies: TDW `Rigidbodies` output data.
        :param target_id: The ID of the object that will play the sound.
        :param resonance: The resonance of the objects.
        :param play_audio_data: If True, return a `play_audio_data` command. If False, return a `play_point_source_data` command (useful only with Resonance Audio; see Command API).
        :param robot_joints: Optional; a list of robot joint IDs in the scene. Use this to correctly play audio day for a robot joint.

        :return A `play_audio_data` or `play_point_source_data` command that can be sent to the build via `Controller.communicate()`.
        """

        impact_audio = self.get_sound(collision, rigidbodies, other_id, other_mat, target_id, target_mat, other_amp, target_amp, resonance)
        if impact_audio is not None:
            return {"$type": "play_audio_data" if play_audio_data else "play_point_source_data",
                    "id": target_id,
                    "num_frames": impact_audio.length,
                    "num_channels": 1,
                    "frame_rate": 44100,
                    "wav_data": impact_audio.wav_str,
                    "robot_joint": robot_joints is not None and target_id in robot_joints,
                    "y_pos_offset": 0.1}
        # If PyImpact failed to generate a sound (which is rare!), fail silently here.
        else:
            return {"$type": "do_nothing"}

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
        for jmat in range(0, len(AudioMaterial)):
            if mat1 == AudioMaterial(jmat):
                tmp1 = AudioMaterial(jmat)
                mat1 = tmp1.name
            if mat2 == AudioMaterial(jmat):
                tmp2 = AudioMaterial(jmat)
                mat2 = tmp2.name
        # Sample modes of object1.
        modes_1 = self.object_modes[id2][id1].obj1_modes
        modes_2 = self.object_modes[id2][id1].obj2_modes
        # Scale the two sounds as specified.
        modes_2.decay_times = modes_2.decay_times + 20 * np.log10(amp2re1)
        snth = PyImpact.synth_impact_modes(modes_1, modes_2, mass, resonance)
        return snth, modes_1, modes_2

    def get_impulse_response(self, collision: Union[Collision, EnvironmentCollision], rigidbodies: Rigidbodies, other_id: int, other_mat: str, target_id: int, target_mat: str, other_amp: float, target_amp: float, resonance: float) -> np.array:
        """
        Generate an impulse response from the modes for two specified objects.

        :param collision: TDW `Collision` or `EnvironmentCollision` output data.
        :param target_mat: The target's audio material.
        :param other_id: The other object's ID.
        :param other_mat: The other object's audio material.
        :param rigidbodies: TDW `Rigidbodies` output data.
        :param target_id: The ID of the object that will play the sound.
        :param other_amp: Sound amplitude of other object.
        :param target_amp: Sound amplitude of target object.
        :param resonance: The resonance of the objects.

        :return The impulse response.
        """
        self.get_sound(collision, rigidbodies, other_id, other_mat, target_id, target_mat, other_amp, target_amp, resonance)

        modes_1 = self.object_modes[target_id][other_id].obj1_modes
        modes_2 = self.object_modes[target_id][other_id].obj2_modes
        h1 = modes_1.sum_modes(resonance=resonance)
        h2 = modes_2.sum_modes(resonance=resonance)
        h = Modes.mode_add(h1, h2)
        return h, min(modes_1.frequencies)

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
    def get_object_info(csv_file: Union[str, Path] = "") -> Dict[str, ObjectAudio]:
        """
        Returns ObjectInfo values.
        As of right now, only a few objects in the TDW model libraries are included. More will be added in time.

        :param csv_file: The path to the .csv file containing the object info. By default, it will load `tdw/py_impact/objects.csv`. If you want to make your own spreadsheet, use this file as a reference.

        :return: A list of default ObjectInfo. Key = the name of the model. Value = object info.
        """

        objects: Dict[str, ObjectAudio] = {}
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
                o = ObjectAudio(name=row["name"], amp=float(row["amp"]), mass=float(row["mass"]),
                                material=AudioMaterial[row["material"]], library=row["library"],
                                bounciness=float(row["bounciness"]), resonance=float(row["resonance"]),
                                size=int(row["size"]))
                objects.update({o.name: o})

        return objects

    @staticmethod
    def get_collisions(resp: List[bytes]) -> Tuple[List[Collision], List[EnvironmentCollision], Optional[Rigidbodies]]:
        """
        Parse collision and rigibody data from the output data.

        :param resp: The response from the build.

        :return: A list of collisions on this frame (can be empty), a list of environment collisions on this frame (can be empty), and Rigidbodies data (can be `None`).
        """

        if len(resp) == 1:
            return [], [], None
        collisions: List[Collision] = []
        environment_collisions: List[EnvironmentCollision] = []
        rigidbodies: Optional[Rigidbodies] = None
        for r in resp[:-1]:
            r_id = OutputData.get_data_type_id(r)
            if r_id == 'coll':
                collisions.append(Collision(r))
            if r_id == 'rigi':
                rigidbodies = Rigidbodies(r)
            if r_id == 'enco':
                environment_collisions.append(EnvironmentCollision(r))

        return collisions, environment_collisions, rigidbodies

    @staticmethod
    def is_valid_collision(collision: Union[Optional[Collision], Optional[EnvironmentCollision]]) -> bool:
        """
        :param collision: Collision or EnvironmentCollision output data from the build.

        :return: True if this collision can be used to generate an impact sound.
        """

        return collision is not None and ((isinstance(collision, Collision) and
                                           np.linalg.norm(collision.get_relative_velocity()) > 0) or
                                          isinstance(collision, EnvironmentCollision))

    def reset(self, initial_amp: float = 0.5) -> None:
        """
        Reset PyImpact. This is somewhat faster than creating a new PyImpact object per trial.

        :param initial_amp: The initial amplitude, i.e. the "master volume". Must be > 0 and < 1.
        """

        assert 0 < initial_amp < 1, f"initial_amp is {initial_amp} (must be > 0 and < 1)."

        # Clear the object data.
        self.object_modes.clear()

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

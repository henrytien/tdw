# AudioInitializerBase

`from tdw.add_ons.audio_initializer_base import AudioInitializerBase`

Abstract base class for an audio initializer add-on.

***

## Fields

- `avatar_id` The ID of the listening avatar.

***

## Functions

#### \_\_init\_\_

**`AudioInitializerBase()`**

**`AudioInitializerBase(avatar_id="a", framerate=60)`**

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| avatar_id |  str  | "a" | The ID of the listening avatar. |
| framerate |  int  | 60 | The target simulation framerate. |

#### get_initialization_commands

**`self.get_initialization_commands()`**

This function gets called exactly once per add-on. To re-initialize, set `self.initialized = False`.

_Returns:_  A list of commands that will initialize this add-on.

#### on_send

**`self.on_send(resp)`**

This is called after commands are sent to the build and a response is received.

Use this function to send commands to the build on the next frame, given the `resp` response.
Any commands in the `self.commands` list will be sent on the next frame.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| resp |  List[bytes] |  | The response from the build. |

#### play

**`self.play(path, position)`**

**`self.play(path, position, audio_id=None)`**

Load a .wav file and prepare to send a command to the build to play the audio.
The command will be sent on the next `Controller.communicate()` call.

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| path |  Union[str, Path] |  | The path to a .wav file. |
| position |  Union[np.array, Dict[str, float] |  | The position of audio source. Can be a numpy array or x, y, z dictionary. |
| audio_id |  int  | None | The unique ID of the audio source. If None, a random ID is generated. |

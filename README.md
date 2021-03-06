# Appdaemon Spotify Player

### Appdaemon app that plays Spotify songs on a Spotify connected device from Home Assistant or Appdaemon.

Easily play music on a Spotify connected device from artists, albums, tracks, and playlists or get recommendations from your 
favourite music without the need to know the Spotify uri. All with a single event!   

This app also includes the functionality to take a snapshot and restore playback using an event. This 
can be useful when making a TTS announcement and restoring the music afterwards.   

## Installation/Configuration Instructions

### Installation:

Copy the contents of [spotify_client.py](https://github.com/AlexLadd/Appdaemon-Spotify-Player/blob/master/spotify_client.py) to
your Appdaemon apps folder. For Appdaemon installation follow the instructions [here](https://appdaemon.readthedocs.io/en/latest/INSTALL.html). 

### Dependencies:
* **spotipy**
* **voluptuous**
* **BeautifulSoup**
* **pychromecast**

### App Configuration:

```yaml
# Example apps.yaml entry
spotify_client:
  module: spotify_client
  class: SpotifyClient
  username: your_spotify_user_name
  password: your_spotify_password

```

#### Optional Parameters:
* **event_domain_name** (Optional - Default: 'spotify'): Customize the domain name of the event (ex: 'my_spotify')
* **debugging** (Optional - Default: False): Enable more verbose logging (True/False)
* **country** (Optional - Default: Spotify account default): ISO 3166-1 alpha-2 country code format (ex: 'CA')
* **language** (Optional - Default: Spotify account default): ISO 639 language code and an ISO 3166-1 alpha-2 country code, joined by an underscore (ex: 'en_CA')
* **user_aliases** (Optional): Map of alias names to Spotify account usernames (Spotify usernames found in your account)
* **device_aliases** (Optional): Map of alias device names to Spotify account device names (Spotify device names found in your account)

```yaml
# Full configuration example apps.yaml entry
spotify_client:
  module: spotify_client
  class: SpotifyClient
  event_domain_name: my_custom_domain
  debugging: True
  username: your_spotify_user_name
  password: your_spotify_password
  country: CA
  language: en_CA
  user_aliases:
    alex : spotify_alex
  device_aliases:
    office : Office Speaker
    master : Master Bedroom Speaker
    living room : Family Room Speaker
    basement : Basement Speaker
    upstairs : Upstairs Speakers
    everywhere : All Speakers
    no bedrooms : All Except Bedrooms
```


## Usage

### Play Event Parameters ('spotify.play'):

**Event name**: If event_domain_name is specified in the app config, the event name will be event_domain_name + '.play' (Default: 'spotify.play')

#### Required:
* **device**: Spotify connected device name (aliases may be used if you have defined device_aliases in the app config)

#### Optional:
**Warning**: If any of the optional parameters are specified they will be considered 'on' or 'True'. If you do not want a
parameter to be used simply remove it from the event call.

* **track**: Spotify track uri or song name
* **album**: Spotify album uri or album name
* **artist**: Spotify artist uri or artist name
* **playlist**: Spotify playlist uri or playlist name

* **username**: Spotify username (aliases may be used if you have defined user_aliases in the app config)
* **genre**: Genre of music to find a recommendation for
* **category**: Category of music to find a recommendation for 
* **featured**: Play a playlist from Spotify featured playlists
* **new_releases**: Play an album from newly released Spotify albums
* **similar**: Find music similar to the input parameters but not the same

* **random_start**: Start at a random position in the playlist, album or list of tracks
* **random_search**: Randomize the search results
* **shuffle**: Set Spotify shuffle state to 'on'
* **repeat**: Set repeat to one of 'track', 'context', 'off'
* **single**: If specified only a single track will play regardless of which other options have been chosen (takes priority over multiple)
* **multiple**: If specified multiple tracks will play
* **tracks**: The desired number of tracks to be played, this is not a guarantee (multiple & single take priority)

### Examples (for Appdaemon)
**Note**: These can all be played from Home Assistant by firing the play event with the added parameters

Play a playlist from a Spotify uri and randomize the starting position  
```self.fire_event('spotify.play', device='office', playlist='spotify:playlist:37i9dQZF1DWXRqgorJj26U', random_start=True)```

Play a track from a Spotify track uri and play multiple songs that are similar afterwards  
```self.fire_event('spotify.play', device='office', track='spotify:track:6mFkJmJqdDVQ1REhVfGgd1', multiple=True)```

Play the playlist called "Alex's songs" from user 'alex' and randomize the starting position  
```self.fire_event('spotify.play', device='office', username='alex', playlist="Alex's songs", random_start=True)```

Play music from the category rock  
```self.fire_event('spotify.play', device='office', category='rock')```

Play a random album from newly released Spotify albums  
```self.fire_event('spotify.play', device='office', new_releases=True, random_search=True)```

Play something from Pink Floyd and make sure multiple songs are played  
```self.fire_event('spotify.play', device='master', artist='Pink Floyd', multiple='on', random_search='on')```

Play a single track only from the album 'The Wall'   
```self.fire_event('spotify.play', device='master', album='The Wall', single='yes please')```

Play an album similar to 'The Wall' but not the same  
```self.fire_event('spotify.play', device='office', album='The Wall', similar=True, random=True)```

Play the album the 'The Wall' with shuffle turned on  
```self.fire_event('spotify.play', device='office', album='The Wall', shuffle=True)```



### Controls Event Parameters ('spotify.controls'):

**Event name**: If event_domain_name is specified, the event name will be event_domain_name + '.controls' (Default: 'spotify.controls')

* **volume_level**: The percent volume level to set the current Spotify device to
* **action**: The desired action
  * **pause**: Pause the current device playback
  * **resume**: Resume the current device playback
  * **stop**: Stop the current device playback (Same as result pause)
  * **next**: Skip to the next track
  * **previous**: Skip to the previous track
  * **increase_volume**: Increase the current device volume level by 5 percent
  * **decrease_volume**: Decrease the current device volume level by 5 percent
  * **mute**: Mute the current device volume
  * **snapshot**: Take a snapshot of what is currently playing on Spotify
  * **restore**: Restore music from a previously taken snapshot (optionally specify the device to restore the music on)
* **device** (Optional): The device to restore the music on when using action='restore' (default will restore to the device the snapshot was taken from)
* **transfer_playback**: Transfer the currently playing music to the specified device

### Examples (for Appdaemon)
**Note**: These can all be played from Home Assistant by firing the controls event with the added parameters

Take a snapshot of the currently playing music on Spotify   
```self.fire_event('spotify.controls', action='snapshot')```

Restore the previously taken snapshot   
```self.fire_event('spotify.controls', action='restore')```

Restore the previously taken snapshot to the office speaker  
```self.fire_event('spotify.controls', action='restore', device='office')```

Play a track from a Spotify track uri and play multiple songs that are similar afterwards   
```self.fire_event('spotify.controls', volume_level='25')```

Play a track from a Spotify track uri and play multiple songs that are similar afterwards   
```self.fire_event('spotify.controls', action='increase_volume')```

Play a track from a Spotify track uri and play multiple songs that are similar afterwards   
```self.fire_event('spotify.controls', action='pause')```

## Contributors
* [Daniel Lashua](http://github.com/dlashua)

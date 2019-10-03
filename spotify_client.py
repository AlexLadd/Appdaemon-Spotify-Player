"""
Appdaemon app to play Spotify songs on a Spotify connected device  using an event fired from Home Assistant or Appdaemon.

See https://github.com/AlexLadd/Appdaemon-Spotify-Player/blob/master/spotify_client.py for configuration and examples.
"""

import appdaemon.plugins.hass.hassapi as hass
import spotipy
from pychromecast.controllers.spotify import SpotifyController
import pychromecast
import random
import datetime
import time
import voluptuous as vol
import requests
from bs4 import BeautifulSoup
import json

CONF_USERNAME = 'username'
CONF_PASSWORD = 'password'
CONF_DEBUGGING = 'debugging'
CONF_COUNTRY = 'country'
CONF_LANGUAGE = 'language'
CONF_USER_ALIASES = 'user_aliases'
CONF_DEVICE_ALIASES = 'device_aliases'
CONF_EVENT_DOMAIN_NAME = 'event_domain_name'

DEFAULT_EVENT_DOMAIN_NAME = 'spotify'
DEFAULT_EVENT_PLAY = '.play'
DEFAULT_EVENT_CONTROLS = '.controls'

DEFAULT_COUNTRY = 'CA'
DEFAULT_LANGUAGE = 'en_CA'

# Max number of times to retry playing a Spotify song
MAX_PLAY_ATTEMPTS = 2

def _is_spotify_country(value):
  """ ISO 3166-1 alpha-2 country code format (ex: 'US') """
  if value is None:
    raise vol.Invalid('country is None.')
  if not isinstance(value, str):
    raise vol.Invalid('country is not a string.')
  if len(value) != 2 or not value.isupper():
    raise vol.Invalid('Invalid country format, please use ISO 3166-1 alpha-2 country code format.')
  return value

def _is_spotify_language(value):
  """ ISO 639 language code and an ISO 3166-1 alpha-2 country code, joined by an underscore (ex: 'en_US') """
  if value is None:
    raise vol.Invalid('language is None.')
  if not isinstance(value, str):
    raise vol.Invalid('language is not a string.')
  if len(value) != 5 or not value[:2].islower() or value[2] != '_' or not _is_spotify_country(value[3:]):
    raise vol.Invalid('Invalid language format, please use an ISO 639 language code and an ISO 3166-1 alpha-2 country code, joined by an underscore.')
  return value

SPOTIFY_CLIENT_SCHEMA = vol.Schema(
  {
    vol.Required(CONF_USERNAME): str,                                               # Spotify username
    vol.Required(CONF_PASSWORD): str,                                               # Spotify password
    vol.Optional(CONF_EVENT_DOMAIN_NAME, default=DEFAULT_EVENT_DOMAIN_NAME): str,   # Change the default event domain name from 'spotify'
    vol.Optional(CONF_DEBUGGING, default=False): bool,                              # Adjust the verbosity of the logging output
    vol.Optional(CONF_COUNTRY, default=DEFAULT_COUNTRY): _is_spotify_country,       # Your country
    vol.Optional(CONF_LANGUAGE, default=DEFAULT_LANGUAGE): _is_spotify_language,    # Your language
    vol.Optional(CONF_USER_ALIASES, default={}): {str: str},                        # Map alias name to Spotify usernames
    vol.Optional(CONF_DEVICE_ALIASES, default={}): {str: str},                      # Map alias device name to Spotify device names
  }, 
  extra=vol.ALLOW_EXTRA
)


class SpotifyClient(hass.Hass):

  def initialize(self):
    config = SPOTIFY_CLIENT_SCHEMA(self.args)
    self._event_domain_name = config.get(CONF_EVENT_DOMAIN_NAME)
    self._event_play = self._event_domain_name + DEFAULT_EVENT_PLAY
    self._event_controls = self._event_domain_name + DEFAULT_EVENT_CONTROLS
    self._debugging = config.get(CONF_DEBUGGING)
    self._username = config.get(CONF_USERNAME)
    self._password = config.get(CONF_PASSWORD)
    self._country = config.get(CONF_COUNTRY)
    self._language = config.get(CONF_LANGUAGE)
    self._user_aliases = {}
    self._device_aliases = {}

    for alias, user in config.get('user_aliases').items():
      self._user_aliases[alias] = user
    for alias, device in config.get('device_aliases').items():
      self._device_aliases[alias] = device

    # AD logs will only show 'INFO' level messages using default settings
    if self._debugging:
      self.DEBUG_LEVEL = 'INFO'
    else:
      self.DEBUG_LEVEL = 'DEBUG'

    if self._event_play != DEFAULT_EVENT_NAME:
      self.log('Spotify play event name has been changed to a custom event name: "{}"'.format(self._event_play), level=self.DEBUG_LEVEL)

    self.sp = None                    # Spotify client object
    self._access_token = None         # Spotify access token
    self._token_expires = None        # Spotify token expiry in seconds
    self._chromecasts = []            # All discovered chromecast devices
    self._spotify_devices = {}        # Spotify device_name -> device_id
    self._last_cast = None            # The cast device that was last used
    self._play_retry_count = 0        # Current number of song replay tries

    # Register the Spotify play event listener
    self.handle_spotify_play = self.listen_event(self._spotify_play_event_callback, event=self._event_play)

    # Register the Spotify play event listener
    self.handle_spotify_play = self.listen_event(self._spotify_controls_event_callback, event=self._event_controls)

    # Spotify web token is valid for 3600 seconds, so renew before 1 hour expires
    self.run_every(self._renew_spotify_token, self.datetime() + datetime.timedelta(seconds=2), 3500)


  def _renew_spotify_token(self, kwargs):
    """ Callback to renew spotify token """
    self._initialize_spotify_client()


  def _initialize_spotify_client(self):
    """ Refresh the Spotify client instance """
    access_token, expires = self._get_spotify_token(self._username, self._password)
    self._access_token = access_token
    self._token_expires = expires

    if not access_token:
      self.log('Did not retrieve token info for spotify.', level='WARNING')
    else:
      self.log('Spotify client initialized.', level=self.DEBUG_LEVEL)
      self.sp = spotipy.Spotify(auth=access_token)


  def _get_spotify_token(self, username, password):
    """ 
    Starts session to get Spotify access token. (Modified version of spotify_token)
    This version logs in as a real web browser - more powerful token
    """
    # arbitrary value and can be static
    cookies = {"__bon": "MHwwfC01ODc4MjExMzJ8LTI0Njg4NDg3NTQ0fDF8MXwxfDE="}
    user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_13_2) \
                  AppleWebKit/537.36 (KHTML, like Gecko) Chrome/63.0.3239.132 Safari/537.36"
    headers = {'user-agent': user_agent}
    
    session = requests.Session()
    response = session.get("https://accounts.spotify.com/login", headers=headers, cookies=cookies)
    response.raise_for_status()
    csrf_token = response.cookies['csrf_token']

    data = {"remember": False, "username": username, "password": password, "csrf_token": csrf_token}
    response = session.post("https://accounts.spotify.com/api/login", data=data, cookies=cookies, headers=headers)
    response.raise_for_status()

    response = session.get("https://open.spotify.com/browse", headers=headers, cookies=cookies)
    response.raise_for_status()
    data = response.content.decode("utf-8")

    xml_tree = BeautifulSoup(data, 'lxml')
    script_node = xml_tree.find("script", id="config")
    config = json.loads(script_node.string)
    
    access_token = config['accessToken']
    expires_timestamp = config['accessTokenExpirationTimestampMs']
    expires_in = int(expires_timestamp) // 1000 - int(time.time())

    return (access_token, expires_in)


  def map_chromecasts(self, device):
    """ Map alias to chromecast device name (friendly media_player name in HA) """
    if device in self._device_aliases:
      return self._device_aliases[device]
    cc_name = self.map_entity_to_chromecast(device)
    if cc_name:
      return cc_name
    return device


  def map_chromecast_to_entity(self, name):
    """ Map chromecast names to media_player entity id """
    for mp in self.get_state("media_player").values():
      if mp['attributes']['friendly_name'] == name:
        return mp['entity_id']
    return None


  def map_entity_to_chromecast(self, entity_id):
    """ Map entity id to chromecast name """
    for mp in self.get_state("media_player").values():
      if mp['entity_id'] == entity_id:
        return mp['attributes']['friendly_name']
    return None


  def _map_spotify_usernames(self, name):
    """ Map alias name to spotify usernames """
    if name in self._user_aliases:
      return self._user_aliases[name]
    return name


  def get_spotify_uri_type(self, uri):
    """ Returns the type of the Spotify uri (ex: artist, playlist, track, album, etc) """
    parts = uri.split(':')
    if len(parts) < 3:
      self.log('Invalid Spotify uri: {}'.format(uri), level='WARNING')
      return ''
    return parts[-2]


  def is_spotify_uri(self, uri, media_type=None):
    """ 
    Check if valid spotify uri

    param uri: Spotify uri (Format: spotify:(track|playlist|artist|album):twenty-two-digits-here)
    param media_type: The uri type ('track', 'playlist', 'artist', 'album') to check for
    """
    if not uri or not isinstance(uri, str):
      return False

    if isinstance(media_type, str):
      media_type = [media_type]
    elif not media_type:
      media_type = ['track', 'playlist', 'artist', 'album']

    parts = uri.split(':')
    if len(parts) == 3 and parts[0] == 'spotify' and parts[1] in media_type and len(parts[2]) == 22:
      return True
    return False

  def is_artist_uri(self, uri):
    """ Test if given uri is a spotify artist """
    return self.is_spotify_uri(uri, 'artist')

  def is_track_uri(self, uri):
    """ Test if given uri is a spotify track """
    return self.is_spotify_uri(uri, 'track')

  def is_playlist_uri(self, uri):
    """ Test if given uri is a spotify playlist """
    return self.is_spotify_uri(uri, 'playlist')

  def is_album_uri(self, uri):
    """ Test if given uri is a spotify album """
    return self.is_spotify_uri(uri, 'album')


  ######################   PLAY SPOTIFY MUSIC METHODS   ########################

  def spotify_play_timer(self, kwargs):
    """ Callback for scheduler calls to call spotify_play """
    self.spotify_play(kwargs['device'], kwargs['uri'], kwargs.get('off_set', None))


  def spotify_play(self, device, uri, offset=None):
    """ 
    Top level call to play spotify song

    param device: the friendly_name of the speaker in spotify and HA (helper function used to map aliases)
    param uri: Spotify track/playlist/artist/album uri/list of tracks
    param offset: Provide offset as an int or track uri to start playback at a particular offset.
    """
    device_name = self.map_chromecasts(device)

    # Only a list of tracks can be played
    if isinstance(uri, list):
      for u in uri:
        if not self.is_track_uri(u):
          self.log("Invalid list of Spotify uri's, the song will not play. Only a list of tracks can be played.".format(uri), level='WARNING')
          return
    else:
      if not self.is_spotify_uri(uri):
        self.log('Invalid Spotify uri: "{}", the song will not play.'.format(uri), level='WARNING')
        return

    # Check to see if we already have the device
    # This include currently connected chromecasts, spotify connect devices, and desktop players
    dev_id = self._get_spotify_device_devid(device_name)

    # We don't already have the device, look for a chromecast
    if dev_id is None:
      # Setup our cast spotify controller using the specified device
      if not self._register_spotify_on_cast_device(device_name):
        if self._play_retry_count < MAX_PLAY_ATTEMPTS:
          self._play_retry_count += 1
          self.run_in(self.spotify_play_timer, 1, device=device, uri=uri, off_set=offset)
        else:
          self._play_retry_count = 0
          self.log('Exceeded max retries, the song ("{}") will not play on "{}".'.format(uri, device), level='WARNING')
        return

      # Look for our device again
      dev_id = self._get_spotify_device_devid(device_name)

    # Play song on spotify device if found
    if dev_id:
      self._play_on_spotify_device(dev_id, uri, offset)
      self._log_action_from_uri(uri, device)
    else:
      self.log('Could not find device "{}" in Spotify, no song will play. Discovered Chromecast devices: {}, Spotify devices: {}' \
        .format(device_name, self.get_found_chromecasts(), ', '.join(self._spotify_devices.keys())), level='WARNING')


  def _get_spotify_device_devid(self, device_name):
    """ 
    Returns the Spotify device id given the name

    param device_name: The Spotify device name
    """
    # Use cached device if possible
    if device_name in self._spotify_devices:
      self.log('Cached Spotify device used.', level=self.DEBUG_LEVEL)
      dev_id = self._spotify_devices[device_name]
    else:
      devs = self.sp.devices()
      dev_id = None
      for d in devs['devices']:
        if d['name'] == device_name:
          self.log('Newly discovered Spotify device found.'.format(devs), level=self.DEBUG_LEVEL)
          self._spotify_devices[device_name] = d['id']
          dev_id = d['id']
          break

    return dev_id


  def _get_chromcast_device(self, device_name=None):
    """ 
    Returns the chromecast device that matches the device_name as a Chromecast object

    param device_name: The chromecast device name (if not supplied this method will set the self._chromecasts = all CC's and return None)
    """
    # Used cached chromecast if possible - This allows spotify_play to execute in about 1 second (versus several seconds)
    if self._chromecasts:
      for cast in self._chromecasts:
        if cast.device.friendly_name == device_name:
          self.log('Cached chromecast device used.', level=self.DEBUG_LEVEL)
          self._last_cast = cast
          return cast

    # Might need to tweak the settings in get_chromecasts
    chromecasts = pychromecast.get_chromecasts(tries=5, retry_wait=5, timeout=30)
    self._chromecasts = chromecasts
    if not device_name:
      return None

    for cast in chromecasts:
      if cast.device.friendly_name == device_name:
        self.log('Newly discovered chromecast found.', level=self.DEBUG_LEVEL)
        self._last_cast = cast
        return cast
    return None


  def _register_spotify_on_cast_device(self, cast_name):
    """ 
    Register Spotify app on given chromecast device 
    
    param cast_name: Chromecast device name
    """
    # Get our required chromecast device
    cast = self._get_chromcast_device(cast_name)
    if not cast:
      self.log('No chromecast device was found with the name: "{}"'.format(cast_name), level='WARNING')
      return False
    cast.wait(timeout=2)

    cast_sc = SpotifyController(self._access_token, self._token_expires)
    cast.register_handler(cast_sc)
    try:
      cast_sc.launch_app(timeout=10)
    except pychromecast.error.LaunchError as e:
      self.log('Error waiting for status response from Spotify device: "{}", retrying shortly.'.format(cast_name), level='ERROR')
      return False
    except pychromecast.error.NotConnected as e:
      self.log('Chromecast connection failed with: {}'.format(e), level='ERROR')
      return False

    # Make sure everything was initialized correctly
    if not cast_sc.is_launched and not cast_sc.credential_error:
      self.log('Failed to launch spotify controller due to timeout', level='ERROR')
      return False
    if not cast_sc.is_launched and cast_sc.credential_error:
      self.log('Failed to launch spotify controller due to credentials error', level='ERROR')
      return False
    
    return True


  def _log_action_from_uri(self, uri, device):
    """ 
    Log Spotify action based on uri and device - For debugging purposes 
    
    param uri: A valid Spotify uri
    param device: Device name music is playing on
    """
    if isinstance(uri, str):
      if self.is_track_uri(uri):
        track = self.get_track_info(uri)
        self.log('Playing: "{}" by "{}" on "{}" speaker.'.format(track['name'], track['artist'], device), level=self.DEBUG_LEVEL)
      elif self.is_playlist_uri(uri):
        pl = self.get_playlist_info(uri)
        self.log('Playing playlist named: "{}" on "{}" speaker.'.format(pl['name'], device), level=self.DEBUG_LEVEL)
      elif self.is_artist_uri(uri):
        artist = self.get_artist_info(uri)
        self.log('Playing music from artist: "{}" on "{}" speaker.'.format(artist['name'], device), level=self.DEBUG_LEVEL)
      elif self.is_album_uri(uri):
        album = self.get_album_info(uri)
        self.log('Playing music from the album: "{}" by "{}" on "{}" speaker.'.format(album['name'], album['artist'], device), level=self.DEBUG_LEVEL)
      else:
        self.log('Playing something unknown: "{}" on "{}" speaker.'.format(uri, device), level=self.DEBUG_LEVEL)
    else:
      if isinstance(uri, list) and self.is_track_uri(uri[0]):
        self.log('Playing {} tracks on "{}" speaker.'.format(len(uri), device), level=self.DEBUG_LEVEL)
      else:
        self.log('Playing something unknown: "{}" on "{}" speaker.'.format(uri, device), level=self.DEBUG_LEVEL)


  def _play_on_spotify_device(self, spotify_device_id, uri, offset=None):
    """ 
    Play music on Spotify device using valid spotify uri (track, playlist, artist, album) and device id 

    param spotify_device_id: Spotify device id of speaker
    param uri: Spotify track/playlist/artist? uri
    param offset: Provide offset as an int or track uri to start playback at a particular offset. (Only works for playlist/albums)
    """
    # Offset format: {“position”: <int>} or {“uri”: “<track uri>”}
    if isinstance(offset, int):
      o = { 'position' : offset }
    elif isinstance(offset, str):
      o = { 'uri' : offset }
    else:
      o = offset

    # self.log('Playing URI: {} on device-id: {}, with offset: {}.'.format(uri, spotify_device_id, o), level=self.DEBUG_LEVEL)
    try:
      if isinstance(uri, str) and uri.find('track') > 0:
        self.sp.start_playback(device_id=spotify_device_id, uris=[uri], offset=o)
      elif isinstance(uri, list) and uri[0].find('track') > 0:
        self.sp.start_playback(device_id=spotify_device_id, uris=uri, offset=o)
      else:
        self.sp.start_playback(device_id=spotify_device_id, context_uri=uri, offset=o)
    except spotipy.client.SpotifyException as e:
      # This can occur when a cached device is used that has been dropped/disconnected from Spotify
      # Could be prevented by never using cached devices with a trade-off of a significant preformance drop
      device_name = next((device for device, id in self._spotify_devices.items() if id == spotify_device_id), 'Device not cached, this should not occur')
      self.log('Error playing music on Spotify device: "{}". Error: {}'.format(device_name, e), level='ERROR')

  ######################   PLAY SPOTIFY MUSIC METHODS END   ########################

  ######################   UTILITY SPOTIFY METHODS   ########################

  @property
  def is_active(self):
    """ Returns if spotify is currently connected to a device """
    return self.sp.current_playback() is not None


  def _spotify_controls_event_callback(self, event_name, data, kwargs):
    """
    Callback for controlling the active Spotify device from HA or AD

    Actions: pause, stop, resume, skip, previous track, set volume (need extra volume_level parameter), increment/decrement volume, mute
    """
    action = data.get('action', None)
    if not action:
      return

    if action == 'pause':
      self.log('Spotify device paused.', level=self.DEBUG_LEVEL)
      self.pause()
    elif action == 'resume':
      self.log('Spotify device resumed.', level=self.DEBUG_LEVEL)
      self.resume()
    elif action == 'stop':
      self.log('Spotify device stopped.', level=self.DEBUG_LEVEL)
      self.pause()
    elif action in ['skip', 'next', 'next_track']:
      self.log('Spotify device skipped track.', level=self.DEBUG_LEVEL)
      self.next_track()
    elif action in ['previous', 'previous_track']:
      self.log('Spotify device skipped to previous track.', level=self.DEBUG_LEVEL)
      self.previous_track()
    elif action in ['adjust_volume', 'set_volume']:
      volume_level = data.get('volume_level', None) or data.get('volume', None)
      if volume_level:
        try:
          self.set_volume(int(volume_level))
          self.log('Set Spotify device volume to "{}" percent.'.format(volume_level), level=self.DEBUG_LEVEL)
        except ValueError:
          self.log('Please specify a volume_level between 1 and 100 to set the Spotify device volume.', level='WARNING')
      else:
        self.log('Please specify the volume_level parameter to set the Spotify device volume.', level='WARNING')
    elif action == 'decrease_volume':
      self.log('Reduced Spotify device volume.', level=self.DEBUG_LEVEL)
      current_volume = self.current_volume
      self.set_volume(current_volume - 5)
    elif action == 'increase_volume':
      self.log('Increased Spotify device volume.', level=self.DEBUG_LEVEL)
      current_volume = self.current_volume
      self.set_volume(current_volume + 5)
    elif action == 'mute':
      self.log('Spotify device was muted.', level=self.DEBUG_LEVEL)
      self.set_volume(0)


  def repeat(self, state, device=None):
    """
    Sets the Spotify device's repeat state

    param state: Desired repeat state (track, context, or off)
    param device: Spotify device id (or name if the spotify_play method has cached the device)
      -> None will set repeat on the current device
    """
    if self.is_active:
      device_id = self.map_chromecasts(device)
      if device_id in self._spotify_devices:
        device_id = self._spotify_devices[device_id]
      self.sp.repeat(state, device_id)


  def repeat_state(self):
    """  Return the repeat state for the current device """
    if self.is_active:
      return self.sp.current_playback().get('repeat_state')
    return None
  

  def shuffle(self, state, device=None):
    """
    Sets the Spotify device's shuffle state

    param state: Desired shuffle state (True/False)
    param device: Spotify device id (or name if the spotify_play method has cached the device) (optional)
      -> None will set shuffle on the current device
    """
    if self.is_active:
      device_id = self.map_chromecasts(device)
      if device_id in self._spotify_devices:
        device_id = self._spotify_devices[device_id]
      self.sp.shuffle(state, device_id)


  def shuffle_state(self):
    """ Return the shuffle state for the current device """
    if self.is_active:
      return self.sp.current_playback().get('shuffle_state')
    return None


  def next_track(self):
    """ Skip to the next track """
    if self.is_active:
      self.sp.next_track()


  def previous_track(self):
    """ Skip to previous track """
    if self.is_active:
      self.sp.previous_track()


  def pause(self):
    """ Pause the playback """
    if self.is_active:
      self.sp.pause_playback()


  def resume(self):
    """ Resume the playback """
    if self.is_active:
      self.sp.start_playback()


  @property
  def current_volume(self):
    """ Returns the current active device volume level in percent """
    return self.sp.current_playback().get('device', {}).get('volume_percent', None)


  def set_volume(self, volume):
    """ 
    Set the volume level on the currently device
    
    param volume: Desired volume level (0 - 1)
    """
    if self.is_active:
      if 1 < volume < 100:
        volume = volume / 100
      self.sp.volume(int(volume*100))


  def seek_track(self, position_ms, device=None):
    """ 
    Seek to position in current track

    param position_ms: Desired track position in milliseconds
    param device: Spotify device id (or name if the spotify_play method has cached the device)
      -> None will set seek position in the current device
    """
    if self.is_active:
      device_id = self.map_chromecasts(device)
      if device_id in self._spotify_devices:
        device_id = self._spotify_devices[device_id]
      self.sp.seek_track(position_ms, device_id)

  ######################   UTILITY SPOTIFY METHODS END   ########################


  ######################   MUSIC RECOMMENDATION METHODS   ########################

  def get_random_track_from_user_playlist(self, user='steph', playlist_name="Stephanie' s songs"):
    """ 
    Return a random track from stephs chosen playlist as a string

    param user: Spotify username
    param playlist_name: Displayed playlist name 
      -> may need to take special characters into consideration (ex: may need space after apostrophe's)
    """
    playlist = self.get_playlists(user, include=playlist_name)
    if playlist:
      pl_info = self.get_playlist_info(playlist[0])
      if pl_info and 'tracks' in pl_info:
        return random.choice(pl_info['tracks'])
    return ''


  def get_recommended_track(self, artist=None, genre=None, track=None, random_song=False, random_artist=False):
    """
    Return a recommended track based on input variables
    """
    track_uri = ''
    if not any([artist, track, genre]):
      pl = self.get_playlists()
      if pl:
        tracks = self.get_playlist_info(random.choice(pl))['tracks']
        if tracks:
          track_uri = random.choice(tracks)

      if not track_uri:
        pl = self.get_featured_playlists()
        if pl:
          tracks = self.get_playlist_info(random.choice(pl))['tracks']
          if tracks:
            track_uri = random.choice(tracks)

    if not track_uri:
      if artist:
        if random_artist:
          track_uris = self.get_recommendations(artists=artist, genres=genre, tracks=track)
        else:
          track_uris = self.get_top_tracks(artist=artist)
      else:
        track_uris = self.get_recommendations(artists=artist, genres=genre, tracks=track)

      if track_uris:
        if random_song:
          track_uri = random.choice(track_uris)
        else:
          track_uri = track_uris[0]

    if not track_uri:
      return ''

    return track_uri


  def get_recommendations(self, artists=None, genres=None, tracks=None, limit=10):
    """
    Returns recommended tracks as a list
    This method will recommend tracks from various artists

    param artist: artist name or artist info (returned from self.get_artist_info() method)
    param genres: genre of music (ex: rock)
    param tracks: spotify track uri (or track name - but may not find corrent track uri?)
    param limit: limit of song in list to return (1-100)
    """
    if not any([artists, genres, tracks]):
      self.log('Please specify one or more of artists, genres, or tracks.', level='WARNING')
      return

    if isinstance(tracks, str):
      if not self.is_track_uri(tracks): # assumes track name was passed in
        tracks = self.get_track_info(tracks)['uri']
      tracks = [tracks]
    if isinstance(genres, str):
      genres = [genres]
    if isinstance(artists, str):
      if not self.is_artist_uri(artists): # assumes artist name was passed in
        artists = self.get_artist_info(artists)['uri']
      artists = [artists]

    results = self.sp.recommendations(seed_artists=artists, seed_genres=genres, seed_tracks=tracks, limit=limit)
    return [u['uri'] for u in results['tracks']]


  def get_recommendation_genre_seeds(self):
    """
    Returns the available genres for the get_recommendations() functions as a list of genres
    """
    return self.sp.recommendation_genre_seeds()['genres']


  def new_releases(self, country=None, limit=20, offset=0):
    """
    Returns new album releases on Spotify as a list of album uri's

    param country: Valid ISO 3166-1 alpha-2 country code
    param limit: The number of categories to return
    param offset: The index of the first item to return
    """
    results = self.sp.new_releases(country=(country or self._country), limit=20, offset=0)
    return [u['uri'] for u in results['albums']['items']]


  def get_playlists_by_category(self, category, country=None, limit=10, offset=0):
    """
    Returns new album releases featured on Spotify as a list or album uri's
    
    param category: A valid Spotify category (can be found from get_categories method)
    param country: Valid ISO 3166-1 alpha-2 country code
    param limit: Number of desired albums
    param offset: The index of the first item to return
    """
    categories = self.get_categories(country=(country or self._country), limit=50) # Limit of 50 should retrieve all categories possible
    if category not in categories:
      self.log('Invalid category: "{}", valid categories are: {}'.format(category, categories), level='WARNING')
      return []

    result = self.sp.category_playlists(category, country=(country or self._country), limit=limit, offset=offset)
    return [u['uri'] for u in result['playlists']['items']]


  def get_categories(self, country=None, locale=None, limit=10, offset=0):
    """
    Returns valid category id's as a list

    param country: Valid ISO 3166-1 alpha-2 country code
    param locale: Desired language (ISO 639 language code and an ISO 3166-1 alpha-2 country code, joined by an underscore)
    param limit: The number of categories to return
    param offset: The index of the first item to return
    """
    result = self.sp.categories(country=(country or self._country), locale=(locale or self._language), limit=limit, offset=offset)
    return [i['id'] for i in result['categories']['items']]


  def get_top_tracks(self, artist, country=None):
    """
    Returns the top 10 top songs from an artist as a list
    This method will recommend tracks from the specified artist

    param artist: Spotify artist uri or artist name
    param country: Valid ISO 3166-1 alpha-2 country code
    """
    if not self.is_artist_uri(artist):
      artist = self.get_artist_info(artist).get('uri', None)

    # We did not find an artist
    if not artist:
      return []

    results = self.sp.artist_top_tracks(artist, country=(country or self._country))
    return [u['uri'] for u in results['tracks']]


  def get_featured_playlists(self, country=None, locale=None, limit=10):
    """
    Returns featured playlists as a list of playlist uri's

    param country: Valid ISO 3166-1 alpha-2 country code
    param locale: Desired language (ISO 639 language code and an ISO 3166-1 alpha-2 country code, joined by an underscore)
    param limit: The number of playlists to return
    """
    res = self.sp.featured_playlists(locale=(locale or self._language), country=(country or self._country), timestamp=datetime.datetime.now().isoformat(), limit=limit)
    return [u['uri'] for u in res['playlists']['items']]


  def get_artist_tracks(self, artist, limit=10, similar=False, random_search=False):
    """
    Return artist or similar artist tracks as a list of uri's

    param artist: Name of the artist or Spotify artist uri
    param limit: Limit of tracks to find
    param similar: Find tracks from similar artists
    param random_search: Randomize the search results
    """
    if not self.is_artist_uri(artist):
      search_artist = self.get_artist_info(artist)['uri']
    else:
      search_artist = artist

    res = []
    if not similar:
      # Find tracks from given artists albums
      artist_albums = self.get_artist_albums(search_artist)
      if artist_albums:
        if random_search:
          random.shuffle(artist_albums)
        for album in artist_albums:
          if len(res) >= limit:
            break
          tracks = self.get_album_tracks(album)
          if tracks:
            for t in tracks:
              if len(res) < limit:
                res.append(t)
              else:
                break
    else:
      # Find tracks from similar artists
      if len(res) < limit:
        related_artists = self.get_related_artists(search_artist)
        if related_artists:
          if random_search:
            random.shuffle(related_artists)
          for artist in related_artists:
            if len(res) >= limit:
              break
            tracks = self.get_top_tracks(artist)
            if tracks:
              for t in tracks:
                if len(res) < limit:
                  res.append(t)
                else:
                  break
    return res

  ######################   MUSIC RECOMMENDATION METHODS END   ########################


  ######################   MUSIC RECOMMENDATION HELPER METHODS   ########################

  def get_related_artists(self, artist):
    """
    Returns artists related to the given artist as a list of uri's 

    param artist: name of the artist or spotify artist uri
    """
    if not self.is_artist_uri(artist):
      artist_uri = self.get_artist_info(artist)['uri']
    else:
      artist_uri = artist

    related = self.sp.artist_related_artists(artist_uri)
    return [u['uri'] for u in related['artists']]


  def get_album_tracks(self, album, limit=50, offset=0):
    """
    Returns the tracks of an album as a list of track uri's

    param album: Spotify album uri
    param limit: The number of tracks to return
    param offset: The index of the first album to return
    """
    if not self.is_album_uri(album):
      self.log('Invalid album: {}'.format(album), level='WARNING')
      return

    results = self.sp.album_tracks(album, limit=limit, offset=offset)
    return [t['uri'] for t in results['items']]


  def get_artist_albums(self, artist, album_type=None, country=None, limit=20, offset=0):
    """
    Returns albums by the given artist as a list or uri's

    param artist: Spotify artist uri or artist name
    param album_type: One of 'album', 'single', 'appears_on', 'compilation' (optional)
    param country: Limit responce to one particular country
    param limit: The number of albums to return
    param offset: The index of the first album to return (1 - 50)
    """ 
    valid_album_types = ['album', 'single', 'appears_on', 'compilation']
    if album_type and album_type not in valid_album_types:
      self.log('Invalid album_type: {}'.format(album_type), level='WARNING')
      return

    if not self.is_artist_uri(artist):
      artist = self.get_artist_info(artist)['uri']
    
    results = self.sp.artist_albums(artist, album_type=album_type, country=(country or self._country), limit=limit, offset=offset)
    return [a['uri'] for a in results['items']]


  def get_current_user_saved_tracks(self):
    """
    Returns saved tracks from the current user as a list of track uri's
    """
    res = self.sp.current_user_saved_tracks()
    return [u['track']['uri'] for u in res['items']]


  def get_all_playlist_tracks_for_user(self, username='me', include_playlist=None, exclude_playlist=None):
    """
    Return all playlist tracks for a user as a list

    param username: Spotify username
    param include: name or uri of playlists to include in the results (optional)
    param exclude: name or uri of playlists to exclude in the results (optional)
    """
    if include_playlist and exclude_playlist:
      self.log('Cannot specify both include and exclude playlists.', level='WARNING')
      return

    if isinstance(exclude_playlist, str):
      exclude_playlist = [exclude_playlist]
    if isinstance(include_playlist, str):
      include_playlist = [include_playlist]

    username = self._map_spotify_usernames(username)
    # playlists = [self.get_playlist_info(pl, username)['tracks'] for pl in self.get_playlists(username, include_playlist, exclude_playlist)]
    # return [track for tracks in playlists for track in tracks]

    res = []
    for pl in self.get_playlists(username, include_playlist, exclude_playlist):
      for track in self.get_playlist_info(pl, username)['tracks']:
        res.append(track)
    return res


  def get_playlists(self, username='me', include=None, exclude=None):
    """
    Returns playlists owned by a given user as a list

    param username: name of user to find playlists for
    param include: name or uri of playlists to include in the results (optional)
    param exclude: name or uri of playlists to exclude in the results (optional)
    """
    if include and exclude:
      self.log('Cannot specify both include and exclude.', level='WARNING')
      return
    
    if include and isinstance(include, str):
      include = [include]
    if exclude and isinstance(exclude, str):
      exclude = [exclude]

    username = self._map_spotify_usernames(username)
    playlists = self.sp.user_playlists(username)

    if include:
      return [pl['uri'] for pl in playlists['items'] if pl['name'] in include or pl['uri'] in include]
    elif exclude:
      return [pl['uri'] for pl in playlists['items'] if pl['name'] not in exclude and pl['uri'] not in exclude]
    else:
      return [pl['uri'] for pl in playlists['items']]


  def get_current_user_playlists(self):
    """
    Return playlist from the user whose credentials were used in the config as a list
    Can also use get_playlists('my_username') as an alternative
    """
    results = self.sp.current_user_playlists(limit=50)
    return [u['uri'] for u in results['items']]


  def get_tracks_from_playlist(self, uri):
    """
    Return the tracks of a playlist as a list
    
    param uri: Spotify playlist uri
    """
    return self.get_playlist_info(uri).get('tracks', [])


  def get_playlist_info(self, playlist, username='me'):
    """
    Returns playlist info as a dictionary

    param playlist: Spotify playlist uri
    param username: The user that the playlist belongs to
    """
    if not self.is_playlist_uri(playlist):
      self.log('Invalid playlist: {}.'.format(playlist), level='WARNING')
      return {}
    
    username = self._map_spotify_usernames(username)
    playlists = self.sp.user_playlist(username, playlist)

    return {
      'name' : playlists['name'],
      'uri' : playlists['uri'],
      'owner_name' : playlists['owner']['display_name'],
      'owner_id' : playlists['owner']['id'],
      'description' : playlists['description'],
      'num_tracks' : playlists['tracks']['total'],
      'tracks' : [t['track']['uri'] for t in playlists['tracks']['items']],
    }


  def get_track_info(self, track, artist=None):
    """
    Returns track info as a dictionary

    param track: Spotify track uri or track name
    param artist: Spotify artist uri or artist name (optional)
    """
    if not self.is_track_uri(track):
      results = None
      if artist:
        results = self.sp.search(q='artist:' + artist + ' track:' + track, type='track', limit=1)
      else:
        results = self.sp.search(q='track:' + track, type='track', limit=1)

      # Check if we found a result
      if results['tracks']['items']:
        track = results['tracks']['items'][0]['uri']
      else:
        return {}

    result = self.sp.track(track)
    return {
      'uri' : track,
      'name' : result['name'],
      'artist' : result['album']['artists'][0]['name'], # This will only get the first artist (potentially multiple per song)
      'artist_uri' : result['album']['artists'][0]['uri'],
      'album_name' : result['album']['name'],
      'album_uri' : result['album']['uri'],
    }


  def get_artist_info(self, artist):
    """
    Returns artist info as a dictionary

    param artist: artist name, artist uri, album uri, or track uri from the artist (not playlist uri at this point)
    """
    if self.is_track_uri(artist):
      artist_uri = self.get_track_info(artist)['artist_uri']
    elif self.is_album_uri(artist):
      artist_uri = self.get_album_info(artist)['artist_uri']
    else:
      artist_uri = artist

    if not self.is_artist_uri(artist_uri):
      results = self.sp.search(q='artist:' + artist_uri, limit=1, type='artist')
      if results['artists']['items']:
        artist_uri = results['artists']['items'][0]['uri']

    if not self.is_artist_uri(artist_uri):
      self.log('Invalid artist: {}.'.format(artist), level='WARNING')
      return {}

    results = self.sp.artist(artist_uri)
    return {
      'name' : results['name'],
      'uri' : results['uri'],
      'genres' : results['genres'],
    }

  
  def get_album_info(self, album, artist=None):
    """
    Returns the artist, album name, number of tracks, album uri, and a list of track uri's as a dictionary

    param album: Spotify album uri or album name
    param artist: Spotify artist uri or artist name (optional)
    """
    if not self.is_album_uri(album):
      results = None
      if artist:
        results = self.sp.search(q='album:' + album + ' artist:' + artist, type='album', limit=1)
      else:
        results = self.sp.search(q='album:' + album, type='album', limit=1)
      album = results['albums']['items'][0]['uri']
    
    result = self.sp.album(album)
    return {
      'uri' : album,
      'num_tracks' : result['total_tracks'],
      'name' : result['name'],
      'artist' : result['artists'][0]['name'], # This will only get the first artist (potentially multiple per song)
      'artist_uri' : result['artists'][0]['uri'],
      'tracks' : [t['uri'] for t in result['tracks']['items']],
    }

  ######################   MUSIC RECOMMENDATION HELPER METHODS END   ########################


  ######################   SPOTIFY PLAY EVENT HANDLING METHODS   ########################

  def _spotify_play_event_callback(self, event_name, data, kwargs):
    """
    Handles events that play a spotify song to a Spotiy device using event firing from HA or AD
    """
    d = data
    
    device = d.get('device', None)                                      # Spotify device name
    if not device:
      self.log('Please specify a device.', level='WARNING')
      return

    single = True if d.get('single', False) else False                  # Only play one track regardless of other options chosen
    multiple = True if d.get('multiple', False) else False              # Play multiple tracks if defined
    random_start = True if d.get('random_start', False) else False      # Start playlist, album, list of tracks from a random position
    random_search = True if d.get('random_search', False) else False    # Choose random tracks/artists/albums/etc throughout the algorithm
    similar = True if d.get('similar', False) else False                # Find recommendations that are similar to the input but not the same
    shuffle = True if d.get('shuffle', False) else False                # Enable shuffle
    repeat = d.get('repeat', 'off')                                     # Enable repeat (options: 'track', 'context', 'off')
    if repeat not in ['track', 'context', 'off']:
      self.log("Invalid repeat state specified: {}, choose one of 'track', 'context', 'off'".format(repeat), level='WARNING')
      repeat = 'off' 
    try:
      num_tracks = int(d.get('number_tracks', 0))                       # The number of tracks a user would like to hear (single and multiple will take priority over this)
    except ValueError:
      self.log('Please specifiy a number for number_tracks.')
      num_tracks = 0

    to_play = self._get_media_from_uri(data)
    if not to_play:
      # No URI has been passed in, make a recommendation
      to_play = self._get_recommendation(data)

    if to_play:
      offset = None
      if single:
        self.log('A single song will play.', level=self.DEBUG_LEVEL)
        to_play = self._get_single_track(to_play, random_search)
      elif multiple:
        self.log('Multiple songs will play.', level=self.DEBUG_LEVEL)
        to_play = self._get_multiple_tracks(to_play)
      elif num_tracks > 0: # User specified a specific number of tracks they would like to hear
        self.log('"{}" tracks have been requested to play.'.format(num_tracks), level=self.DEBUG_LEVEL)
        to_play = self._get_number_of_tracks(to_play, num_tracks, similar, random_search)
      if random_start:
        self.log('Random start is turned on.', level=self.DEBUG_LEVEL)
        offset = self._get_random_offset(to_play)
      self.spotify_play(device, to_play, offset)
      self.repeat(repeat)
      self.shuffle(shuffle)
    else:
      self.log('Nothing was found matching your parameters. No music will play.', level='INFO')


  def _get_media_from_uri(self, data):
    """
    Checks for a Spotify uri in the data and return the track/playlist/artist/album if found
    Priority uri order track -> playlist -> album -> artist

    param data: The event data from the spotify.play event call
    """
    d = data 

    track = d.get('track', None)
    playlist = d.get('playlist', None)
    album = d.get('album', None)
    artist = d.get('artist', None)

    multiple = True if d.get('multiple', False) else False            # Play multiple tracks if defined
    similar = True if d.get('similar', False) else False              # Find recommendations that are similar to the input but not the same
    random_search = True if d.get('random_search', False) else False

    # If the user is looking for something similar, do not play the uri
    if similar: 
      return ''

    to_play = None

    if track:
      valid = True
      if isinstance(track, list):
        for u in track:
          if not self.is_track_uri(u):
            valid = False
      elif not self.is_track_uri(track):
        valid = False
      elif self.is_track_uri(track) and multiple: # User wants multiple songs
        return ''
      if valid:
        self.log('Playing spotify track(s).', level=self.DEBUG_LEVEL)
        to_play = track
    elif playlist:
      if self.is_playlist_uri(playlist):
        self.log('Playing spotify playlist.', level=self.DEBUG_LEVEL)
        to_play = playlist
    elif album:
      if self.is_album_uri(album):
        self.log('Playing spotify album.', level=self.DEBUG_LEVEL)
        to_play = album
    elif artist:
      if self.is_artist_uri(artist):
        self.log('Playing spotify artist.', level=self.DEBUG_LEVEL)
        to_play = artist
        if random_search:
          albums = self.get_artist_albums(artist)
          to_play = random.choice(albums)

    return to_play


  def _get_recommendation(self, data):
    """
    Returns a Spotify recommendation based on user input data
    Priority order: user defined playlist -> track defined -> album defined -> artist defined -> genre -> category -> 
      -> featured playlist -> newly released album -> users playlist (nothing defined - fallback)

    param data: The data from the spotify.play event call
    """
    d = data

    track = d.get('track', None)
    playlist = d.get('playlist', None)
    album = d.get('album', None)
    artist = d.get('artist', None)

    random_search = True if d.get('random_search', False) else False  # Randomize the search
    user = d.get('username', 'me')                                    # Spotify username
    genre = d.get('genre', None)                                      # Find all genres from get_recommendation_genre_seeds()
    category = d.get('category', None)                                # Find all categories from get_categories()
    featured = True if d.get('featured', False) else False            # Get featured playlist
    new_releases = True if d.get('new_releases', False) else False    # Get newly released albums
    single = True if d.get('single', False) else False                # Only play one track regardless of other options chosen
    multiple = True if d.get('multiple', False) else False            # Play multiple tracks if defined
    similar = True if d.get('similar', False) else False              # Find recommendations that are similar to the input but not the same

    to_play = None

    # Use the user defined playlist parameter to find music
    if playlist:
      self.log('Attempting to use a playlist name find a user playlist.', level=self.DEBUG_LEVEL)
      pl = self.get_playlists(username=user, include=playlist)
      if pl:
        if random_search:
          to_play = random.choice(pl)
        else:
          to_play = pl[0]

    # Use the user defined track parameter to find music
    if not to_play and track:
      if not similar:
        self.log('Attempting to use the track name to find the song.', level=self.DEBUG_LEVEL)
        to_play = self.get_track_info(track, artist).get('uri', None)
      if not to_play:
        self.log('Attempting to use the track name to make a similar track recommendation.', level=self.DEBUG_LEVEL)
        to_play = self.get_recommended_track(tracks=track, genre=genre, artists=artist, random_song=random_search)

    # Use the user defined album parameter to find music
    if not to_play and album:
      if not similar:
        self.log('Attempting to use the album name to find the album.', level=self.DEBUG_LEVEL)
        to_play = self.get_album_info(album, artist).get('uri', None)
      elif similar or not to_play:
        self.log('Attempting to use the album name to find a similar album.', level=self.DEBUG_LEVEL)
        album_info = self.get_album_info(album, artist)
        album_artist = album_info.get('artist_uri', None)
        album_uri = album_info.get('uri', None)
        if album_artist:
          chosen_artist = album_artist
          if random.choice([1,2]) == 1: # Randomly pick a related artist
            self.log('Attemping to use a different artist than the input album artist.', level=self.DEBUG_LEVEL)
            related_artists = self.get_related_artists(album_artist)
            if related_artists:
              if random_search:
                chosen_artist = random.choice(related_artists)
              else:
                chosen_artist = related_artists[0]
          artist_albums = self.get_artist_albums(chosen_artist)
          if album_uri in artist_albums and len(artist_albums) > 1: # Remove the user defined album from the choices
            artist_albums.remove(album_uri)
          if random_search:
            to_play = random.choice(artist_albums)
          else:
            to_play = artist_albums[0]

    # Use the user defined artist parameter to find music
    if not to_play and artist:
      self.log('Attempting to use the artist name to find music.', level=self.DEBUG_LEVEL)
      chosen_artist = artist
      if similar:
        self.log('Attempting to find similar music from the artist.', level=self.DEBUG_LEVEL)
        artist_info = self.get_artist_info(artist)
        similar_artists = self.get_related_artists(artist_info['uri'])
        if similar_artists:
          if random_search:
            chosen_artist = random.choice(similar_artists)
          else:
            chosen_artist = similar_artists[0]

      if single or not multiple:
        to_play = self.get_top_tracks(chosen_artist)
        if random_search:
          to_play += self.get_artist_tracks(chosen_artist, 10, similar, random_search)
          random.shuffle(to_play)
      if (not single and multiple) or not to_play:
        artist_albums = self.get_artist_albums(chosen_artist)
        if artist_albums:
          if random_search:
            to_play = random.choice(artist_albums)
          else:
            to_play = artist_albums[0]

      if not to_play:
        to_play = self.get_recommended_track(artist=artist, genre=genre, random_song=random_search)

    # Use the user defined genre parameter to find music
    if not to_play and genre:
      self.log('Attempting to use the genre name to make a recommendation.', level=self.DEBUG_LEVEL)
      if genre in self.get_categories(limit=50):
        to_play = self.get_playlists_by_category(category)
        if to_play:
          if random_search:
            to_play = random.choice(to_play)
          else:
            to_play = to_play[0]
      elif genre in self.get_recommendation_genre_seeds():
        to_play = self.get_recommendations(artists=artist, genres=genre, tracks=track)

    # Use the user defined category parameter to find music
    if not to_play and category:
      self.log('Attempting to use the category name to make a recommendation.', level=self.DEBUG_LEVEL)
      to_play = self.get_playlists_by_category(category)
      if to_play:
        if random_search:
          to_play = random.choice(to_play)
        else:
          to_play = to_play[0]
      if not to_play:
        to_play = self.get_recommendations(genres=category)

    # Use the user defined featured parameter to find music
    if not to_play and featured:
      self.log('Attempting to use the find featured playlists.', level=self.DEBUG_LEVEL)
      to_play = self.get_featured_playlists() # List of playlists
      if not to_play:
        to_play = self.new_releases() # List of albums
      if to_play:
        if random_search:
          to_play = random.choice(to_play)
        else:
          to_play = to_play[0]

    # Use the user defined new_releases parameter to find music
    if not to_play and new_releases:
      self.log('Attempting to use the find newly released albums.', level=self.DEBUG_LEVEL)
      to_play = self.new_releases() # List of albums
      if not to_play:
        to_play = self.get_featured_playlists() # List of playlists
      if to_play:
        if random_search:
          to_play = random.choice(to_play)
        else:
          to_play = to_play[0]

    # Nothing matches the user defined parameters or none were defined - Fallback
    if not to_play:
      self.log('No Inputs were matched, using fallback which is a saved user playlist or tracks.', level=self.DEBUG_LEVEL)
      to_play = self.get_playlists()
      if to_play:
        if random_search:
          to_play = random.choice(to_play)
        else:
          to_play = to_play[0]
      else:
        to_play = self.get_current_user_saved_tracks()

    return to_play


  def _get_multiple_tracks(self, uri):
    """
    Returns a list of recommended tracks if the uri is a track

    param uri: A valid Spotify uri or list of uri's
    """
    if isinstance(uri, str) and self.is_track_uri(uri):
      tracks = self.get_recommendations(tracks=uri)
      return [uri] + tracks
    return uri


  def _get_number_of_tracks(self, uri, num_tracks, similar=False, random_search=False):
    """
    Returns a list of tracks that is the specified length using the uri for recommendations
    The return number of tracks is not a guarantee

    param uri: A valid Spotify uri or list of track uri's
    param num_tracks: Number of songs requested
    similar: Find tracks from similar artists
    param random_search: Whether or not to randomly choose songs
    """
    res = []

    if isinstance(uri, list):
      res = uri
      if random_search:
        search_uri = random.choice(uri)
      else:
        search_uri = uri[0]
    else: # single uri passed in
      search_uri = uri
      if self.is_track_uri(uri):
        res.append(uri)
      elif self.is_playlist_uri(uri):
        pl_tracks = self.get_playlist_info(search_uri).get('tracks', [])
        res += pl_tracks
      elif self.is_album_uri(uri):
        album_tracks = self.get_album_info(uri).get('tracks', [])
        res += album_tracks
      # If uri is an artist uri, we will get tracks later

    if len(res) > num_tracks:
      return random.sample(res, num_tracks)
    elif len(res) == num_tracks:
      return res

    # Determine the artist of the given uri
    search_artist = None
    if self.is_playlist_uri(search_uri):
      tracks = self.get_playlist_info(search_uri)['tracks']
      if tracks:
        if random_search:
          t = random.choice(tracks)
        else:
          t = tracks[0]
        search_artist = self.get_artist_info(t).get('uri', None)
    else:
      search_artist = self.get_artist_info(search_uri).get('uri', None)

    # Add artist albums tracks until we reach our desired number of tracks
    if search_artist:
      tracks = self.get_artist_tracks(search_artist, num_tracks-len(res), similar, random_search)
      res += tracks
      if len(res) < num_tracks:
        tracks = self.get_artist_tracks(search_artist, num_tracks-len(res), not similar, random_search)
        res += tracks
      if len(res) < num_tracks:
        tracks = self.get_recommendations(artists=search_artist, limit=num_tracks-len(res))
        res += tracks

    return res


  def _get_single_track(self, uri, random_track=False):
    """ 
    Returns a single track regardless of the Spotify media type (playlist, artist, album, track) 
    
    param uri: A valid Spotify uri
    param random_track: Choose a random track or not
    """
    # If a list of tracks/playlist/albums is passed in, deal with it
    if isinstance(uri, list):
      if random_track:
        uri = random.choice(uri)
      else:
        uri = uri[0]

    if self.is_track_uri(uri):
      return uri
    if self.is_playlist_uri(uri):
      tracks = self.get_playlist_info(uri)['tracks']
    if self.is_album_uri(uri):
      tracks = self.get_album_info(uri)['tracks']
    elif self.is_artist_uri(uri):
      albums = self.get_artist_albums(uri)
      if random_track:
        album = random.choice(albums)
      else:
        album = album[0]
      tracks = self.get_album_info(album)['tracks']
    
    if random_track:
      return random.choice(tracks)
    else:
      return tracks[0]


  def _get_random_offset(self, uri):
    """ 
    Return a random offset that corresponds to a valid Sptotify media type (playlist, artist, album, track, or list of tracks) 
    Only playlists, albums and lists of tracks can have an offset

    param uri: A valid Spotify uri
    """
    if isinstance(uri, list):
      nt = len(uri)
    elif self.is_playlist_uri(uri):
      nt = self.get_playlist_info(uri)['num_tracks']
    elif self.is_album_uri(uri):
      nt = self.get_album_info(uri)['num_tracks']
    else:
      return None
    
    return random.randint(0, nt - 1)

  ######################   SPOTIFY PLAY EVENT HANDLING METHODS END   ########################


  def terminate(self):
    pass



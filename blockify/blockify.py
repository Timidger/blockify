"""blockify

Usage:
    blockify [-l <path>] [-v...] [-q] [-h]

Options:
    -l, --log=<path>  Enables logging to the logfile/-path specified.
    -q, --quiet       Don't print anything to stdout.
    -v                Verbosity of the logging module, up to -vvv.
    -h, --help        Show this help text.
    --version         Show current version of blockify.
"""
# TODO: Try xlib/_net for minimized window detection.
# TODO: Play local mp3s when ads are muted? Currently only possible with pulse.
# TODO: Fix fallback mode from pulse to alsa.
import codecs
import logging
import os
import re
import signal
import subprocess
import sys
import time

import gtk
import pygtk
import wnck
import blockifydbus


try:
    from docopt import docopt
except ImportError:
    print "ImportError: Please install docopt to use the CLI."


pygtk.require("2.0")
log = logging.getLogger("main")


class Blocklist(list):
    "Inheriting from list type is a bad idea. Let's see what happens."
    # Could subclass UserList.UserList here instead which inherits from
    # collections.MutableSequence. In Python3 it's collections.UserList.

    def __init__(self):
        super(Blocklist, self).__init__()
        self.home = os.path.expanduser("~")
        self.location = os.path.join(self.home, ".blockify_list")
        self.extend(self.load())
        self.timestamp = self.get_timestamp()

    def append(self, item):
        "Overloading list.append to automatically save the list to a file."
        # Only allow nonempty strings.
        if item in self or not item or item == " ":
            log.debug("Not adding empty or duplicate item: {}.".format(item))
            return
        log.info("Adding {} to {}.".format(item, self.location))
        super(Blocklist, self).append(item)
        self.save()

    def remove(self, item):
        log.info("Removing {} from {}.".format(item, self.location))
        try:
            super(Blocklist, self).remove(item)
            self.save()
        except ValueError as e:
            log.warn("Could not remove {} from blocklist: {}".format(item, e))

    def find(self, song):
        while len(song) > 4:
            for item in self:
                if item.startswith(song):
                    return item
            song = song[:len(song) / 2]

    def get_timestamp(self):
        return os.path.getmtime(self.location)

    def load(self):
        log.debug("Loading blockfile from {}.".format(self.location))
        try:
            with codecs.open(self.location, "r", encoding="utf-8") as f:
                blocklist = f.read()
        except IOError:
            with codecs.open(self.location, "w+", encoding="utf-8") as f:
                blocklist = f.read()

        return [i for i in blocklist.split("\n") if i]

    def save(self):
        log.debug("Saving blocklist to {}.".format(self.location))
        with codecs.open(self.location, "w", encoding="utf-8") as f:
            f.write("\n".join(self) + "\n")
        self.timestamp = self.get_timestamp()


class Blockify(object):

    def __init__(self, blocklist):
        try:
            # Test if spotify is running
            subprocess.check_output(["pidof", "spotify"])
        except subprocess.CalledProcessError:
            log.error("Process not found. Is Spotify running?")
            sys.exit()
        self._automute = True
        self.connect_dbus()
        self.dbus_song = None
        self.blocklist = blocklist
        self.orglist = blocklist[:]
        self.channels = self.get_channels()
        self.current_song = ""
        self.company_list = self.get_company_list()

        # Determine if we can use sinks or have to use alsa.
        try:
            devnull = open(os.devnull)
            subprocess.check_output(["pacmd", "list-sink-inputs"], stderr=devnull)
            self.mutemethod = self.pulsesink_mute
        except (OSError, subprocess.CalledProcessError):
            self.mutemethod = self.alsa_mute

        log.info("Blockify initialized.")

    def connect_dbus(self):
        try:
            self.dbus = blockifydbus.BlockifyDBus()
            self.use_dbus = True
            self._autodetect = True
        except Exception as e:
            log.error("Cannot connect to DBus. Autodetection and Player Controls"
                      " will be unavailable ({}).".format(e))
            self.dbus = None
            self.use_dbus = False
            self._autodetect = False

    def is_ad_playing(self):
        return self.current_song != self.dbus_song

    def update(self):
        "Main loop. Checks for blocklist match and mutes accordingly."
        # It all relies on current_song.
        self.current_song = self.get_current_song()
        if self.use_dbus:
            self.dbus_song = self.dbus.get_song_artist() + u" \u2013 " + \
            self.dbus.get_song_title()

        # Manual control is enabled so we return here.
        if not self.automute:
            return

        # No song playing, unmute.
        if not self.current_song:
            self.toggle_mute(2)
            return

        if self.autodetect and self.use_dbus:
            if self.is_ad_playing():
                self.toggle_mute(1)
                return True

        # Check if the blockfile has changed.
        current_timestamp = self.blocklist.get_timestamp()
        if self.blocklist.timestamp != current_timestamp:
            log.info("Blockfile changed. Reloading.")
            self.blocklist.__init__()

        in_company_list = any(company_name in self.current_song
                              for company_name in self.company_list)

        if self.current_song in self.blocklist or in_company_list:
            self.toggle_mute(1)
            return True  # Return boolean to use as self.found in GUI.
        else:
            self.toggle_mute()

        self.toggle_mute()
        return False

    def get_windows(self):
        "Libwnck list of currently open windows."
        # Get the current screen.
        screen = wnck.screen_get_default()

        # Object list of windows in screen.
        windows = screen.get_windows()

        # Return the actual list of windows or an empty list.
        return [win.get_icon_name() for win in windows if len(windows)]

    def get_current_song(self):
        "Checks if a Spotify window exists and returns the current songname."
        pipe = self.get_windows()
        for line in pipe:
            if line.startswith("Spotify - "):
                # Remove "Spotify - " and return the rest of the songname.
                return " ".join(line.split()[2:])

        # No song playing, so return an empty string.
        return ""

    def get_company_list(self):
        home = os.path.expanduser("~")
        location = os.path.join(home, ".blockify_company_list")
        with codecs.open(location, "r", encoding="utf-8") as companies_file:
            company_list = [line.strip()
                            for line in companies_file.readlines()]
            return company_list

    def block_current(self):
        if self.current_song:
            self.blocklist.append(self.current_song)

    def unblock_current(self):
        if self.current_song:
            song = self.blocklist.find(self.current_song)
            if song:
                self.blocklist.remove(song)
            else:
                log.error("Not found in blocklist or block pattern too short.")

    def get_channels(self):
        channel_list = ["Master"]
        amixer_output = subprocess.check_output("amixer")
        if "'Speaker',0" in amixer_output:
            channel_list.append("Speaker")
        if "'Headphone',0" in amixer_output:
            channel_list.append("Headphone")

        return channel_list

    def toggle_mute(self, mode=0):
        # 0 = automatic, 1 = force mute, 2 = force unmute
        self.mutemethod(mode)

    def is_muted(self):
        for channel in self.channels:
            output = subprocess.check_output(["amixer", "get", channel])
            if "[off]" in output:
                return True
        return False

    def get_state(self, mode):
        muted = self.is_muted()

        state = None
        if mode == 2 or (not self.current_song and muted):
            state = "unmute"
        elif not muted and mode == 1:
            state = "mute"
            log.info("Muting {}.".format(self.current_song))
        elif muted and not mode:
            state = "unmute"
            log.info("Unmuting.")

        return state

    def alsa_mute(self, mode):
        "Mute method for systems without Pulseaudio. Mutes sound system-wide."
        state = self.get_state(mode)
        if not state:
            return

        for channel in self.channels:
            subprocess.Popen(["amixer", "-q", "set", channel, state])

    def pulse_mute(self, mode):
        "Used if pulseaudio is installed but no sinks are found. System-wide."
        state = self.get_state(mode)
        if not state:
            return

        for channel in self.channels:
            subprocess.Popen(["amixer", "-qD", "pulse", "set", channel, state])

    def pulsesink_mute(self, mode):
        "Finds spotify's audio sink and toggles its mute state."
        try:
            pacmd_out = subprocess.check_output(["pacmd", "list-sink-inputs"])
            pidof_out = subprocess.check_output(["pidof", "spotify"])
        except subprocess.CalledProcessError:
            log.error("Sink or process not found. Is Pulse/Spotify running?")
            log.error("Resorting to amixer as mute method.")
            self.mutemethod = self.pulse_mute  # Fall back to amixer mute mode.
            return

        pattern = re.compile(r"(?: index|muted|application\.process\.id).*?(\w+)")
        pids = pidof_out.decode("utf-8").strip().split(" ")
        output = pacmd_out.decode("utf-8")

        # Every third element is a key, the value is the preceding two
        # elements in the form of a tuple - {pid : (index, muted)}
        info = pattern.findall(output)
        idxd = {info[3 * n + 2]: (info[3 * n], info[3 * n + 1])
                for n in range(len(info) // 3)}

        try:
            pid = [k for k in idxd.keys() if k in pids][0]
            index, muted = idxd[pid]
        except IndexError:
            return

        if mode == 2 or (not self.current_song and muted == "yes"):
            log.info("Forcing unmute.")
            subprocess.call(["pacmd", "set-sink-input-mute", index, "0"])
        elif muted == "no" and mode == 1:
            log.info("Muting {}.".format(self.current_song))
            subprocess.call(["pacmd", "set-sink-input-mute", index, "1"])
        elif muted == "yes" and not mode:
            log.info("Unmuting.")
            subprocess.call(["pacmd", "set-sink-input-mute", index, "0"])

    def bind_signals(self):
        "Catch SIGINT and SIGTERM to exit cleanly & SIGUSR1 to block a song."
        signal.signal(signal.SIGUSR1, lambda sig, hdl: self.block_current())
        signal.signal(signal.SIGUSR2, lambda sig, hdl: self.unblock_current())
        signal.signal(signal.SIGTERM, lambda sig, hdl: self.stop())
        signal.signal(signal.SIGINT, lambda sig, hdl: self.stop())

    def stop(self):
        log.info("Exiting safely. Bye.")
        # Save the list only if it changed during runtime.
        if self.blocklist != self.orglist:
            self.blocklist.save()
        # Unmute before exiting.
        self.toggle_mute(2)
        sys.exit()

    @property
    def automute(self):
        return self._automute

    @automute.setter
    def automute(self, boolean):
        log.debug("Setting automute to: {}.".format(boolean))
        self._automute = boolean

    @property
    def autodetect(self):
        return self._autodetect

    @autodetect.setter
    def autodetect(self, boolean):
        log.debug("Setting autodetect to: {}.".format(boolean))
        self._autodetect = boolean


def init_logger(logpath=None, loglevel=1, quiet=False):
    "Initializes the logging module."
    logger = logging.getLogger()

    # Set the loglevel.
    if loglevel > 3:
        loglevel = 3  # Cap at 3 to avoid index errors.
    levels = [logging.ERROR, logging.WARN, logging.INFO, logging.DEBUG]
    logger.setLevel(levels[loglevel])

    logformat = "%(asctime)-14s %(levelname)-8s %(name)-8s %(message)s"

    formatter = logging.Formatter(logformat, "%Y-%m-%d %H:%M:%S")

    if not quiet:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        log.debug("Added logging console handler.")
        log.info("Loglevel is {}.".format(levels[loglevel]))
    if logpath:
        try:
            logfile = os.path.abspath(logpath)
            file_handler = logging.FileHandler(logfile)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            log.debug("Added logging file handler: {}.".format(logfile))
        except IOError:
            log.error("Could not attach file handler.")


def main():
    "Entry point for the CLI-version of Blockify."
    try:
        args = docopt(__doc__, version="1.1")
        init_logger(args["--log"], args["-v"], args["--quiet"])
    except NameError:
        init_logger(logpath=None, loglevel=2, quiet=False)
        log.error("Please install docopt to use the CLI.")

    blocklist = Blocklist()
    blockify = Blockify(blocklist)

    blockify.bind_signals()
    blockify.toggle_mute()

    while True:
        # Initiate gtk loop to enable the window list for .get_windows().
        while gtk.events_pending():
            gtk.main_iteration(False)
        blockify.update()
        time.sleep(0.5)


if __name__ == "__main__":
    main()

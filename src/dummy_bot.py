from __future__ import annotations
from console import Console
from threading import Event

class DummyBot:
    def __init__(self, motd_event: Event, response_event: Event):
        self.motd_event = motd_event
        self.response_event = response_event
        motd_event.set()

    def clear_response_event(self):
        self.response_event.clear()

    def send_bot_command(self, msg):
        Console.writeln(f"DummyBot send_bot_command: '{msg}'")
        self.response_event.set()

    def send_message(self, channel: str, content: str):
        Console.writeln(f"DummyBot send_message: '{channel}' -> '{content}'")
        self.response_event.set()

    def send_pm(self, user: str, content: str):
        Console.writeln(f"DummyBot send_pm: '{user}' -> '{content}'")
        self.response_event.set()

    def send_raw(self, content: str):
        Console.writeln(f"DummyBot send_raw: '{content}'")
        self.response_event.set()

    def join_channel(self, channel: str):
        Console.writeln(f"DummyBot join_channel: '{channel}'")
        self.response_event.set()

    def close_room(self, warn=True):
        Console.writeln(f"DummyBot close_room. warn={warn}")
        self.response_event.set()

    def stop(self):
        Console.writeln(f"DummyBot stop")
        self.response_event.set()

    def shutdown(self):
        Console.writeln(f"DummyBot shutdown")
        self.response_event.set()

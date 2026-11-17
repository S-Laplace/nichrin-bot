"""What the routes are wired to on a host that is actually running KakaoTalk.

The HTTP layer knows nothing about a heap scan, a window handle, or a name
ledger; it knows it was handed callables. These are those callables, so the
platform-specific half of the server is one import away from the rest of it.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator, Optional

from ..errors import ErrorCode, KanaoError
from .config import UPLOAD_KEEP_SECONDS


class SharedReads:
    """One snapshot cache and one key ring for every read the server answers.

    Ported from kamado (nichirin #93). A reader built with neither decrypted
    the catalog and the room's log again on each request, and overlapping
    requests each held their own decrypted copy until the read slots ran out.
    Sharing the cache makes the first read pay for the decryption and every
    later one a fingerprint check, or a log overlay when only the log moved.

    ``SnapshotCache`` takes no lock, so every use of the shared one happens
    under :attr:`lock`; the first request decrypts while the rest wait, then
    find the snapshot cached.

    Windows differs from the Mac in where the keys come from: each room has
    its own key, found in the running client's heap, and a room the client
    opens later is not in a key ring found before it. A read's rescan is kept
    for the next read, and a read that ends ``KEY_UNAVAILABLE`` drops the
    shared ring, so the read the handler retries after ``open=true`` looks
    for the account again instead of reusing one that cannot know the room.
    """

    def __init__(self) -> None:
        from ..snapshot import SnapshotCache

        self.lock = threading.RLock()
        self._snapshots = SnapshotCache()
        self._account: dict[str, Any] = {}

    def open_reader(self) -> Any:
        """A fresh reader on the shared cache; use it only under :attr:`lock`.

        Like the reads it replaces it records no names: the ledger stays the
        send line's.
        """
        from ..reader import Reader

        return Reader(cache=self._snapshots, keyring=self._account.get("keyring"))

    @contextlib.contextmanager
    def reading(self) -> Iterator[Any]:
        """Hold the line for one read; the reader itself is fresh each time."""
        with self.lock:
            reader = self.open_reader()
            try:
                yield reader
            except KanaoError as error:
                if error.code == ErrorCode.KEY_UNAVAILABLE:
                    self._account.pop("keyring", None)
                else:
                    self._keep_keyring(reader)
                raise
            else:
                self._keep_keyring(reader)
            finally:
                reader.close()

    def _keep_keyring(self, reader: Any) -> None:
        found = getattr(reader, "_keyring", None)
        if found is not None:
            self._account["keyring"] = found

    def update_keyring(self, found: Any) -> None:
        """Make keys discovered by SSE available to HTTP reads as well."""
        from ..keyring import Keyring

        with self.lock:
            current = self._account.get("keyring")
            if current is None or current.pid != found.pid:
                self._account["keyring"] = found
                return
            merged = dict(current.keys)
            merged.update(found.keys)
            if merged != current.keys:
                self._account["keyring"] = Keyring(
                    user_directory=found.user_directory, pid=found.pid, keys=merged,
                )

    def warm(self) -> None:
        """Decrypt the catalog once so the first request does not wait for it.

        Failure is only reported: a client that is closed or signed out should
        still serve ``/health`` and explain itself on the request that needs it.
        """
        try:
            with self.reading() as reader:
                reader.rooms()
        except Exception as error:  # noqa: BLE001 - the request path retries
            print(
                f"warning: warming the snapshot failed: {type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )

    def warm_in_background(self) -> threading.Thread:
        thread = threading.Thread(target=self.warm, name="kanao-warm", daemon=True)
        thread.start()
        return thread


def make_live_rooms_provider(reader_factory) -> Callable[[], list[Any]]:
    """Return the rooms a live server lists on each ``GET /rooms``."""

    def provider() -> list[Any]:
        with reader_factory() as reader:
            return list(reader.rooms())

    return provider


def make_live_context_provider(reader_factory) -> Callable[..., dict[str, Any]]:
    """Return the context a live server reads on each ``GET /context``.

    The reader is new per request, so revealing covered messages for one
    caller cannot leak into another's answer.
    """

    def provider(room_id: int, limit: int, *, show_covered: bool = False) -> dict[str, Any]:
        from ..context import read_context

        with reader_factory() as reader:
            reader.reveal_covered = show_covered
            answer = read_context(reader, room_id, limit=limit)
        return {key: value for key, value in answer.items() if key != "ok"}

    return provider


def make_live_room_opener(
    reader_factory, on_keys: Optional[Callable[[Any], None]] = None,
) -> Callable[[int], None]:
    """Open a room and publish newly found keys before the HTTP read retries.

    The opener has its own reader so it does not hold the shared snapshot lock
    while driving KakaoTalk. Its successful rescan must also update HTTP reads:
    the next request would otherwise reuse an older shared key ring.
    """

    def opener(room_id: int) -> None:
        from ..opening import open_for_reading

        with reader_factory() as reader:
            room = reader.room(room_id)
            if room is None:
                raise KanaoError(ErrorCode.CHAT_NOT_FOUND, f"no room {room_id} in the catalog")
            if room.readable:
                return
            open_for_reading(reader, room)
            if on_keys is not None:
                on_keys(reader.keyring)

    return opener


def _live_writer() -> Callable[..., dict[str, Any]]:
    """Build what the server's writes, text and files, go through on a Windows host.

    Each call opens a fresh reader, resolves the room exactly like the ``send``
    command, refuses duplicate catalog titles, and hands the room to the real
    sender — but the snapshot cache and the key ring outlive the request.

    Without that, every ``POST /send`` decrypted the whole catalog and scanned
    the client's heap again before a single character reached the input box,
    which is the rule ``AGENTS.md`` states as *a send must not re-read the
    catalog*: ``reader.rooms()`` measured 3.0 s against a stale snapshot, and
    sending is exactly what makes it stale. Sharing the cache alone is not
    enough — finding the account again is most of a second by itself.

    Freshness is not what is given up.  ``SnapshotCache.connect`` re-reads the
    fingerprint on every call and lays the log over cached pages when only the
    log moved, so a shared cache is the mechanism working rather than being
    bypassed; and the reader itself is still new each time, so nothing it
    memoises about names or columns carries into the next request.

    ``SnapshotCache`` takes no locks, and sends run one at a time under the
    handler's send lock, so this cache needs no lock of its own. The read
    routes share a different cache under a lock of theirs (:class:`SharedReads`).
    """
    from ..names import NameLedger
    from ..reader import Reader
    from ..snapshot import SnapshotCache

    snapshots = SnapshotCache()
    account: dict[str, Any] = {}
    """The key ring the last send found, kept for the next one.

    It is the reader that finds it, on the first send rather than at startup:
    scanning when the socket is bound would turn a closed client into a server
    that does not come up, instead of one that comes up and says so.
    """

    def write(room: str, act: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        from ..context import _resolve
        from ..model import SELF_CHAT
        from ..opening import search_rows
        from ..winauto import make_sender

        with Reader(
            cache=snapshots, keyring=account.get("keyring"), ledger=NameLedger()
        ) as reader:
            # Reading the property is what scans, and the next line would have
            # scanned anyway; taking it here is where the answer gets kept.
            account["keyring"] = reader.keyring
            # cli._selected_room is this line plus an argparse namespace built
            # to carry two values; importing cli from here is a cycle, and the
            # cycle is the only thing that namespace was for.
            #
            # openapi.json calls `room` "the room's title, or its id as a
            # string", and a JSON body carries both as a string, while
            # `_resolve` takes the id path for an int alone. A client sending
            # back the id `/rooms` gave it was answered CHAT_NOT_FOUND. The id
            # is tried first and only when it names a room, so a room whose
            # title is a number still resolves by title.
            resolved = None
            if room.strip().isdigit():
                resolved = reader.room(int(room.strip()))
            if resolved is None:
                resolved = _resolve(reader, room)
            catalogued = reader.rooms()
            duplicates = [
                r for r in catalogued if r.title.casefold() == resolved.title.casefold()
            ]
            if len(duplicates) != 1:
                raise KanaoError(
                    ErrorCode.AMBIGUOUS_CHAT,
                    "multiple catalog rooms share this window title; "
                    "give the target a unique title",
                    candidates=[r.as_json() for r in duplicates],
                )
            stored = reader.stored_title(resolved.id)
            window_aliases = [stored] if stored and stored != resolved.title else []
            position = next(
                (index for index, r in enumerate(catalogued) if r.id == resolved.id),
                None,
            )
            try:
                result = act(
                    make_sender(),
                    resolved.title,
                    aliases=window_aliases,
                    hint=position,
                    # The client's search lists rooms that merely have a
                    # member by that name above the room itself, so the row
                    # to open is predicted from the catalog just read.
                    search_rows=search_rows(reader, resolved, window_aliases, catalogued),
                    self_chat=resolved.type == SELF_CHAT,
                )
            except OSError as error:
                raise KanaoError(ErrorCode.SEND_FAILED, str(error)) from error
            return {**result, "roomId": str(resolved.id)}

    return write


def make_live_send() -> Callable[..., dict[str, Any]]:
    """Build the send callable the server uses on a Windows host."""
    write = _live_writer()

    def send(
        room: str,
        text: str,
        dry_run: bool = False,
        open_if_needed: bool = False,
        aliases=(),
        hint=None,
    ) -> dict[str, Any]:
        return write(
            room,
            lambda sender, title, **where: sender.send(
                title, text, dry_run=dry_run, open_if_needed=open_if_needed, **where
            ),
        )

    return send


def make_live_upload() -> Callable[..., dict[str, Any]]:
    """Build the file-send callable the server uses on a Windows host.

    The bytes are written once under the name the room will see, in a folder
    of their own, and the client is pointed at that path the way a person
    points it at a file. The copy outlives the request because the client
    uploads from it after its confirmation has closed.
    """
    write = _live_writer()

    def upload(
        room: str, name: str, data: bytes, open_if_needed: bool = False
    ) -> dict[str, Any]:
        path = stage_upload(_upload_folder(), name, data, now=time.time())
        return write(
            room,
            lambda sender, title, **where: sender.upload(
                title, str(path), open_if_needed=open_if_needed, **where
            ),
        )

    return upload


def _upload_folder() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "kanao-cli" / "uploads"


def stage_upload(folder: Path, name: str, data: bytes, *, now: float) -> Path:
    """Write *data* as *name* in a fresh folder under *folder*, sweeping old ones."""
    sweep_uploads(folder, now=now)
    target = folder / secrets.token_hex(8)
    target.mkdir(parents=True)
    path = target / name
    path.write_bytes(data)
    return path


def sweep_uploads(folder: Path, *, now: float) -> None:
    """Remove staged copies older than UPLOAD_KEEP_SECONDS.

    A copy the client still holds open cannot be removed on Windows; it is
    left for the next sweep rather than failing the upload that found it.
    """
    if not folder.is_dir():
        return
    for entry in folder.iterdir():
        try:
            if entry.is_dir() and now - entry.stat().st_mtime > UPLOAD_KEEP_SECONDS:
                shutil.rmtree(entry)
        except OSError:
            continue


def make_live_digest_provider(reader_factory) -> Callable[..., dict[str, Any]]:
    """Return the digest a live server builds on each ``GET /digest``.

    The digest is ``kanao.digest.build``, the same call the command makes, so
    a remote caller and a local one are reading one implementation rather than
    two. Reaching into ``cli`` for it used to mean assembling an
    ``argparse.Namespace`` here and pointing this package at the layer above
    it.
    """

    def provider(
        room_id: int, span: str, limit: int, highlights: int, *, show_covered: bool = False
    ) -> dict[str, Any]:
        from ..digest import build

        with reader_factory() as reader:
            reader.reveal_covered = show_covered
            return build(
                reader, room_id, span=span, limit=limit, highlights=highlights
            )

    return provider


def _room_of(reader, room_id: int):
    room = reader.room(room_id)
    if room is None:
        raise KanaoError(ErrorCode.CHAT_NOT_FOUND, f"no room {room_id} in the catalog")
    return room


def make_live_notice_provider(reader_factory) -> Callable[[int], dict[str, Any]]:
    """Return the pinned notice a live server reads on each ``GET /notice``."""

    def provider(room_id: int) -> dict[str, Any]:
        with reader_factory() as reader:
            room = _room_of(reader, room_id)
            pinned = reader.notice(room_id)
            return {
                "room": room.as_json(),
                "notice": None if pinned is None else pinned.as_json(),
            }

    return provider


def make_live_attachments_provider(reader_factory) -> Callable[..., dict[str, Any]]:
    """Return what a live server lists on each ``GET /attachments``."""

    def provider(
        room_id: int, kind: Any, limit: int, before_id: Any
    ) -> dict[str, Any]:
        with reader_factory() as reader:
            room = _room_of(reader, room_id)
            found = reader.attachments(room_id, kind=kind, limit=limit, before_id=before_id)
            return {
                "room": room.as_json(),
                "attachments": [item.as_json() for item in found],
                "count": len(found),
            }

    return provider


def make_live_revisions_provider(reader_factory) -> Callable[[int, int], dict[str, Any]]:
    """Return an edited message's versions on each ``GET /revisions``."""

    def provider(room_id: int, message_id: int) -> dict[str, Any]:
        with reader_factory() as reader:
            room = _room_of(reader, room_id)
            versions = reader.revisions(room_id, message_id)
            return {
                "room": room.as_json(),
                "messageId": str(message_id),
                "revisions": [version.as_json() for version in versions],
                "count": len(versions),
            }

    return provider


def make_live_file_provider(reader_factory) -> Callable[[int, int], Any]:
    """Return what ``GET /file`` streams: the client's saved copy, or the address.

    The reader is closed before a byte is sent; the download it found is a
    file or a response, and it is the handler's to close.
    """

    def provider(room_id: int, message_id: int):
        from ..files import open_file

        with reader_factory() as reader:
            _room_of(reader, room_id)
            attachment = reader.file_at(room_id, message_id)
        return open_file(attachment)

    return provider


_KEY_RESCAN_FIRST_SECONDS = 30.0
_KEY_RESCAN_MAX_SECONDS = 180.0
_NEW_ROOM_BACKFILL_SECONDS = 300


class _SharedKeyRefresh:
    """One adaptive heap scan schedule shared by this server's SSE connections."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ring: Any = None
        self.delay = _KEY_RESCAN_FIRST_SECONDS
        self.next_due = time.monotonic() + self.delay

    def update(self, reader: Any) -> None:
        from ..keyring import Keyring

        with self.lock:
            now = time.monotonic()
            if now >= self.next_due:
                # Set the next due date even when the scan raises.
                self.delay = min(self.delay * 2, _KEY_RESCAN_MAX_SECONDS)
                self.next_due = now + self.delay
                try:
                    discovered = Keyring.discover(reader.user_directory, rescan=True)
                    previous = self.ring if self.ring is not None else reader.keyring
                except Exception as error:
                    print(
                        f"warning: SSE key refresh failed: {type(error).__name__}",
                        file=sys.stderr, flush=True,
                    )
                else:
                    if discovered.pid is None and previous.pid is not None:
                        # A temporary failure to find the process is not a
                        # reason to erase all previously working keys.
                        pass
                    elif previous.pid == discovered.pid:
                        combined = dict(previous.keys)
                        combined.update(discovered.keys)
                        self.ring = Keyring(
                            user_directory=discovered.user_directory,
                            pid=discovered.pid, keys=combined,
                        )
                        if any(
                            name not in previous.keys or previous.keys[name] != key
                            for name, key in discovered.keys.items()
                        ):
                            self.delay = _KEY_RESCAN_FIRST_SECONDS
                            self.next_due = now + self.delay
                    else:
                        # Never carry keys across KakaoTalk process changes.
                        self.ring = discovered
                        self.delay = _KEY_RESCAN_FIRST_SECONDS
                        self.next_due = now + self.delay

            if self.ring is not None:
                reader._keyring = self.ring


def make_live_watcher_factory(
    reader_factory,
    interval: float,
    lock: Optional[Callable[[], ContextManager[Any]]] = None,
    on_keys: Optional[Callable[[Any], None]] = None,
) -> Callable[[], Any]:
    """Return one ``watch.Watcher`` per SSE connection on a live server.

    *lock* is entered around every step that touches the database, for a
    reader whose cache is shared with other requests (see :class:`SharedReads`).
    """
    guard: Callable[[], ContextManager[Any]] = lock if lock is not None else contextlib.nullcontext
    keys = _SharedKeyRefresh()

    class _LiveWatcher:
        """Own a reader; share key scans without sharing message cursors."""

        def __init__(self) -> None:
            self._reader = reader_factory()
            from ..watch import Watcher

            self._watcher = Watcher(self._reader, interval=interval)
            self._started = time.time()
            self._initialized = False
            self._known_rooms: set[int] = set()
            self._retry_rooms: dict[int, float] = {}

        def _remember_rooms(self) -> None:
            # A key may be present even when the room's initial database read
            # failed. Only a successfully established cursor is "known":
            # the other rooms still need their first recent-message recovery.
            seen = getattr(self._watcher, "_seen", None)
            if isinstance(seen, dict):
                self._known_rooms.update(seen)
            else:
                watched = getattr(self._watcher, "watched_rooms", None)
                if callable(watched):
                    self._known_rooms.update(watched())

        def prime(self) -> None:
            with guard():
                self._watcher.prime()
                self._remember_rooms()
                self._initialized = True

        def resume(self, cursor) -> None:
            with guard():
                self._watcher.resume(cursor)
                self._remember_rooms()
                self._initialized = True

        def poll(self):
            with guard():
                if not self._initialized:
                    # Watcher.poll() implicitly primes on its first call.
                    # That initial poll must never backfill historical rooms.
                    result = self._watcher.poll()
                    self._remember_rooms()
                    self._initialized = True
                    return result
                keys.update(self._reader)
                if on_keys is not None:
                    on_keys(self._reader.keyring)
                backfill = getattr(self._watcher, "backfill_new_room", None)
                watched = getattr(self._watcher, "watched_rooms", None)
                recovered = []
                if callable(backfill) and callable(watched):
                    now = time.monotonic()
                    # A bounded recent window prevents re-playing years of
                    # history while keeping a command sent before key discovery.
                    since = int(max(
                        self._started, time.time() - _NEW_ROOM_BACKFILL_SECONDS
                    ))
                    for room_id in set(watched()) - self._known_rooms:
                        if now < self._retry_rooms.get(room_id, 0):
                            continue
                        try:
                            recovered.extend(backfill(room_id, since=since))
                        except Exception as error:
                            self._retry_rooms[room_id] = now + 30.0
                            print(
                                f"warning: SSE room {room_id} backfill failed: "
                                f"{type(error).__name__}",
                                file=sys.stderr, flush=True,
                            )
                        else:
                            self._known_rooms.add(room_id)
                            self._retry_rooms.pop(room_id, None)
                return recovered + self._watcher.poll()

        def close(self) -> None:
            try:
                with guard():
                    self._reader.close()
            except Exception:
                pass

    def factory() -> Any:
        return _LiveWatcher()

    return factory

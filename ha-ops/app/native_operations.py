"""Inert durable native workflow primitives; deliberately unused by legacy dispatch.

Callers must supply fresh, complete adapter observations. This module performs
no Git, Supervisor, inventory, or live filesystem operations.
"""
import copy
import bisect
import fcntl
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 2
CHUNK_SIZE = 65536
DEFAULT_RETENTION = 32
_OMITTED = object()
DOMAINS = {"migration", "scope", "save", "apply"}
STATUSES = {"preview", "reviewed", "consumed", "stale", "cancelled", "completed", "recovery_required"}
PHASES = {"accepted", "effect_started", "completed", "failed", "invalidated", "recovery_required"}
WORKER_TRANSITIONS = {"accepted": {"effect_started", "failed", "invalidated"},
                      "effect_started": {"completed", "recovery_required"},
                      "recovery_required": {"completed", "recovery_required"}}
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class InvalidState(ValueError):
    """Unsupported/corrupt data is retained, never replaced with defaults."""


class Conflict(RuntimeError):
    """Revision, authority, or worker ownership no longer matches."""


def _require(condition, message):
    if not condition:
        raise InvalidState(message)


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("utf-8")
    except (ValueError, TypeError) as exc:
        raise InvalidState("noncanonical JSON") from exc


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _hash(value):
    _require(isinstance(value, str) and DIGEST.fullmatch(value), "invalid digest")


def _id(value):
    try:
        _require(isinstance(value, str) and str(uuid.UUID(value)) == value, "invalid ID")
    except (ValueError, AttributeError) as exc:
        raise InvalidState("invalid ID") from exc


def _int(value):
    _require(type(value) is int and value >= 0, "invalid integer")


def _fields(value, names):
    _require(isinstance(value, dict) and set(value) == set(names.split()), "invalid fields")


def _text(value):
    _require(isinstance(value, str) and 0 < len(value) <= 1024 and "\x00" not in value, "invalid text")


def _path(value, absolute=False):
    _text(value)
    _require(value.startswith("/") == absolute and "\\" not in value,
             "invalid path")
    if absolute and value == "/":
        return
    parts = value[1:].split("/") if absolute else value.split("/")
    _require(all(part and part not in {".", ".."} for part in parts), "invalid path")


# Only explicitly implemented execution profiles select a byte identity codec.
_NATIVE_PROFILES = {"native-publication-v1": "native-bytes-v1", "native-scope-v1": "native-bytes-v1", "native-scope-v2": "native-bytes-v1", "native-apply-v1": "native-bytes-v1"}


def identity_codec(identity):
    return _NATIVE_PROFILES.get(identity["execution_policy"], "legacy-text-v1")


def encode_native_path(raw, *, absolute=False):
    _require(type(raw) is bytes and bool(raw) and b"\0" not in raw,
             "invalid native byte path")
    _require(raw.startswith(b"/") == absolute, "invalid native byte path")
    if absolute and raw == b"/":
        return "/nb1"
    parts = (raw[1:] if absolute else raw).split(b"/")
    _require(all(part and part not in (b".", b"..") for part in parts),
             "invalid native byte component")
    return ("/nb1/" if absolute else "nb1/") + "/".join(part.hex() for part in parts)


def decode_native_path(token, *, absolute=False):
    _require(type(token) is str, "invalid native byte token")
    if absolute and token == "/nb1":
        return b"/"
    marker = "/nb1/" if absolute else "nb1/"
    _require(token.startswith(marker), "unsupported native byte token")
    parts = token[len(marker):].split("/")
    _require(all(re.fullmatch(r"(?:[0-9a-f]{2})+", part) for part in parts),
             "noncanonical native byte token")
    raw = (b"/" if absolute else b"") + b"/".join(bytes.fromhex(part) for part in parts)
    _require(encode_native_path(raw, absolute=absolute) == token, "native byte token mismatch")
    return raw


def native_membership(path, scope):
    decode_native_path(path)
    decode_native_path(scope)
    return path == scope or path.startswith(scope + "/")


def _identity_path(path, codec="legacy-text-v1", *, absolute=False):
    if codec == "native-bytes-v1":
        decode_native_path(path, absolute=absolute)
    else:
        _require(codec == "legacy-text-v1", "unsupported identity codec")
        _path(path, absolute=absolute)


def _timestamp(value):
    _text(value)
    try:
        parsed = datetime.fromisoformat(value)
        _require(parsed.tzinfo is not None, "timestamp needs timezone")
    except ValueError as exc:
        raise InvalidState("invalid timestamp") from exc


def _scope(value, codec="legacy-text-v1"):
    _fields(value, "revision allowlist exclude_digest")
    _int(value["revision"])
    _hash(value["exclude_digest"])
    _require(isinstance(value["allowlist"], list), "invalid allowlist")
    for path in value["allowlist"]:
        _identity_path(path, codec)
    if codec == "native-bytes-v1":
        ordered = sorted(value["allowlist"])
        _require(not any(b.startswith(a + "/") for a, b in zip(ordered, ordered[1:])), "redundant native scope")
    _require(value["allowlist"] == sorted(set(value["allowlist"])), "unsorted allowlist")


def _root(value, codec="legacy-text-v1"):
    _fields(value, "path device inode mount_digest topology_digest")
    _identity_path(value["path"], codec, absolute=True)
    _int(value["device"])
    _int(value["inode"])
    _hash(value["mount_digest"])
    _hash(value["topology_digest"])


def _refs(value):
    _fields(value, "base local remote")
    for ref in value.values():
        _fields(ref, "name oid")
        _text(ref["name"])
        name = ref["name"]
        # git-check-ref-format rules, without invoking Git from the state API.
        _require(name.startswith("refs/") and not name.endswith(("/", ".")) and
                 not any(char in name for char in " ~^:?*[\\") and
                 not any(ord(char) < 32 or ord(char) == 127 for char in name) and
                 ".." not in name and "@{" not in name and
                 all(part and not part.startswith(".") and not part.endswith(".lock")
                     for part in name.split("/")), "invalid ref")
        _require(isinstance(ref["oid"], str) and OID.fullmatch(ref["oid"]), "invalid OID")


def validate_identity(value, domain):
    keys = "direction execution_policy scope refs root paths effect"
    if domain == "migration":
        keys += " migration"
    if domain == "scope":
        keys += " scope_transaction"
    _fields(value, keys)
    _require(domain in DOMAINS and value["direction"] == domain, "wrong domain")
    _text(value["execution_policy"])
    codec = identity_codec(value)
    _scope(value["scope"], codec)
    _refs(value["refs"])
    _root(value["root"], codec)
    _fields(value["effect"], "before after")
    for effect in value["effect"].values():
        _hash(effect)
    _require(isinstance(value["paths"], list), "invalid paths")
    paths = []
    for item in value["paths"]:
        _fields(item, "path type mode content_digest metadata_digest")
        _identity_path(item["path"], codec)
        paths.append(item["path"])
        _require(item["type"] in {"file", "directory", "symlink", "missing"}, "invalid type")
        if item["type"] == "missing":
            _require(all(item[k] is None for k in ("mode", "content_digest", "metadata_digest")), "invalid absence")
        else:
            _int(item["mode"])
            _require(item["mode"] <= 0o177777, "invalid mode")
            expected_type = {"file": stat.S_IFREG, "directory": stat.S_IFDIR, "symlink": stat.S_IFLNK}[item["type"]]
            _require(stat.S_IFMT(item["mode"]) == expected_type, "mode/type mismatch")
            _hash(item["metadata_digest"])
            if item["type"] == "directory":
                _require(item["content_digest"] is None, "directory content")
            else:
                _hash(item["content_digest"])
    _require(paths == sorted(set(paths)), "unsorted paths")
    if domain == "migration":
        extra = value["migration"]
        _fields(extra, "original_checkout candidate_commit post_refs")
        _root(extra["original_checkout"], codec)
        _require(isinstance(extra["candidate_commit"], str) and OID.fullmatch(extra["candidate_commit"]), "invalid commit")
        _refs(extra["post_refs"])
    if domain == "scope":
        extra = value["scope_transaction"]
        _fields(extra, "old new")
        _scope(extra["old"], codec)
        _scope(extra["new"], codec)
        _require(extra["old"] == value["scope"] and
                 extra["new"]["revision"] == extra["old"]["revision"] + 1, "invalid scope transition")


def _decisions(value, identity):
    _require(isinstance(value, dict), "invalid decisions")
    allowed = {item["path"] for item in identity["paths"]}
    _require(set(value) <= allowed and all(choice in {"ha", "git"} for choice in value.values()), "invalid decisions")
    if identity["direction"] in {"save", "apply"}:
        roots = identity["scope"]["allowlist"]
        if identity_codec(identity) == "native-bytes-v1":
            # Scope is validated and has no redundant ancestors. The closest
            # canonical predecessor is the only possible covering component root.
            def inside(path):
                index = bisect.bisect_right(roots, path) - 1
                return index >= 0 and (path == roots[index] or path.startswith(roots[index] + "/"))
            _require(all(inside(path) for path in value), "selection outside declared scope")
        else:
            _require(all(any(path == root or path.startswith(root + "/") for root in roots)
                         for path in value), "selection outside declared scope")


def _approved_decisions(value, identity):
    _decisions(value, identity)
    _require(bool(value) or identity["direction"] in {"migration", "scope"}, "empty selection")


def _binding(record, decisions):
    payload = {"identity": record["identity"], "artifact": record["artifact"], "decisions": decisions}
    if record.get("binding_version", 1) == 2:
        payload["artifact"] = {"digest": record["artifact"], "size": record["artifact_size"]}
        payload["namespace"] = {"instance_id": record["instance_id"], "generation": record["generation"]}
    return digest(payload)


def _evidence(value):
    _fields(value, "before after marker")
    _hash(value["before"])
    _hash(value["after"])
    _id(value["marker"])


def publication_request_commitment(record,worker):
    return digest({'profile':'native-publication-request-v1','artifact':record['artifact'],
        'identity_digest':record['identity_digest'],'operation_id':worker['id'],'owner':worker['owner'],
        'epoch':worker['epoch'],'marker':worker['marker']})


def _publication_outcome(value,record,worker):
    _fields(value,'kind mode owner epoch tip graph transcript request')
    _id(value['owner']); _int(value['epoch'])
    _require(0<value['epoch']<=worker['epoch'],'invalid publication outcome epoch')
    _require(value['kind'] in ('known-not-sent','no-update','published'),'invalid native publication outcome')
    _require(value['mode'] in ('original-push','readback'),'invalid publication outcome mode')
    if value['mode']=='readback':
        _require(value['kind']=='published' and value['transcript'] is None,'readback cannot invent transcript')
    else: _hash(value['transcript'])
    width=len(record['identity']['refs']['remote']['oid'])
    _require(type(value['tip']) is str and re.fullmatch('[0-9a-f]{%d}'%width,value['tip']) and value['tip'].strip('0'),
        'invalid refusal observed tip width')
    for field in ('graph','request'): _hash(value[field])
    source=dict(worker,owner=value['owner'],epoch=value['epoch'])
    _require(value['request']==publication_request_commitment(record,source),'refusal request commitment mismatch')


def publication_refusal_evidence(record, worker, kind):
    _require(kind in {"known-not-sent", "no-update"}, "unproven publication refusal")
    outcome=worker['publication_outcome']
    _publication_outcome(outcome,record,worker)
    _require(outcome['kind']==kind,'refusal kind mismatch')
    return {"before": record["identity"]["effect"]["before"],
            "after": digest({"profile":"native-publication-refusal-v1", "artifact":record["artifact"],
                "identity_digest":record["identity_digest"], "operation_id":worker["id"],
                "epoch":worker["epoch"], "kind":kind,'outcome':outcome}), "marker":worker["marker"]}


def _publication_outcome_template(record,worker,kind,mode='original-push'):
    # Accounting inputs only; never actual outcome/state outputs.
    return {'kind':kind,'mode':mode,'owner':worker['owner'],'epoch':worker['epoch'],
        'tip':'f'*len(record['identity']['refs']['remote']['oid']),
        'graph':'f'*64,'transcript':None if mode=='readback' else 'f'*64,'request':publication_request_commitment(record,worker)}


def _publication_refused(record,worker):
    return (record["identity"]["execution_policy"] == "native-publication-v1" and
            worker["phase"] == "failed" and worker.get('publication_outcome') is not None and
            worker["evidence"] == publication_refusal_evidence(record,worker,worker['publication_outcome']['kind']))


def _publication_abandonment_cleanup(state,worker):
    """Durable no-send abandonment permits only explicit terminal settlement.

    Absence and an empty resource token do not confer ownership. Require the
    original proved refusal and its recorded explicit abandonment command;
    lifecycle may transfer cleanup authority without changing either evidence.
    """
    outcome=worker.get('publication_outcome')
    if (worker.get('publication_attempt')!='not-sent' or outcome is None or
            outcome['kind']!='known-not-sent' or worker['phase']!='failed'):
        return False
    record=state['records'][worker['record_id']]
    if not _publication_refused(record,worker):
        return False
    return any(receipt['operation_id']==worker['id'] and receipt['request_version']==3 and
        receipt['pending_effect']=='abandon' and receipt['generation']==record['generation'] and
        (receipt['owner'],receipt['epoch'])==(outcome['owner'],outcome['epoch']) and
        receipt['payload_digest']==_publication_command_payload(record,worker,receipt)
        for receipt in state['commands'].values())


def _record(value, domain, schema=1):
    names = "id domain identity artifact identity_digest status decisions approval created_at validated_at checkpoints"
    if schema == 2:
        names += " instance_id generation artifact_size binding_version recovery_range"
        _id(value["instance_id"])
        _int(value["generation"])
        _require(value["generation"] > 0, "invalid candidate generation")
        _int(value["artifact_size"])
        _require(type(value["binding_version"]) is int and value["binding_version"] in {1, 2}, "invalid binding version")
    _fields(value, names)
    _id(value["id"])
    _require(value["domain"] == domain and value["status"] in STATUSES, "invalid record status")
    validate_identity(value["identity"], domain)
    _hash(value["artifact"])
    _require(value["identity_digest"] == digest(value["identity"]), "identity mismatch")
    _timestamp(value["created_at"])
    if value["validated_at"] is not None:
        _timestamp(value["validated_at"])
    if value["status"] in {"reviewed", "consumed", "completed", "recovery_required"}:
        _approved_decisions(value["decisions"], value["identity"])
        _fields(value["approval"], "id binding")
        _id(value["approval"]["id"])
        _require(value["approval"]["binding"] == _binding(value, value["decisions"]), "approval mismatch")
    else:
        _decisions(value["decisions"], value["identity"])
        _require(value["approval"] is None and not value["decisions"], "unexpected authority")
    _require(isinstance(value["checkpoints"], list), "invalid checkpoints")
    for checkpoint in value["checkpoints"]:
        names="operation_id owner epoch phase evidence"
        if value['identity']['execution_policy']=='native-publication-v1':
            names+=' publication_attempt publication_outcome publication_cleanup'
            _require(type(checkpoint['publication_cleanup']) is int and 0<=checkpoint['publication_cleanup']<=4,'invalid cleanup checkpoint')
            _require(checkpoint['publication_attempt'] in ('not-sent','possible'),'invalid checkpoint attempt marker')
            if checkpoint['publication_outcome'] is not None:
                _require(checkpoint['phase'] in ('effect_started','recovery_required','completed','failed'),'wrong outcome phase')
        _fields(checkpoint,names)
        _id(checkpoint["operation_id"])
        _id(checkpoint["owner"])
        _int(checkpoint["epoch"])
        _require(checkpoint["phase"] in PHASES, "invalid checkpoint")
        if checkpoint["evidence"] is not None:
            _evidence(checkpoint["evidence"])


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def _parse(data):
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_require(False, "nonfinite JSON")))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise InvalidState("invalid native state JSON") from exc


def _now():
    return datetime.now(timezone.utc).isoformat()


class NativeOperationStore:
    """Private /data store. Opening/reading is observational, not a takeover."""

    def __init__(self, data_root, owner=None, fault=None, retention=DEFAULT_RETENTION):
        self.data_root = Path(data_root).absolute()
        self.owner = owner or str(uuid.uuid4())
        _id(self.owner)
        self.fault = fault or (lambda stage: None)
        _int(retention)
        self.retention = retention
        self.cleanup_errors = []
        self._prepared_publication = None
        with _LOCKS_GUARD:
            self.mutex = _LOCKS.setdefault(str(self.data_root), threading.RLock())

    @contextmanager
    def apply_authority(self,prepared,*,capability=None):
        from native_disk import require_apply
        from native_apply import PreparedApply
        if type(prepared) is not PreparedApply or prepared.issuer.operations is not self:raise Conflict('issued Apply authority required')
        require_apply(capability,self,prepared.operation)
        prepared.check(fresh=False)
        yield capability

    def _apply_gate(self,identity,capability=None):
        if identity['execution_policy']=='native-apply-v1':
            from native_disk import require_apply
            require_apply(capability,self)
            from native_disk import check_apply_identity
            check_apply_identity(capability,self,identity)

    def apply_cleanup_result(self,prepared,result,*,release=False,capability=None):
        from native_disk import require_apply
        require_apply(capability,self,prepared.operation,deleting=True)
        """Durable terminal cleanup outcome; never reruns configuration effects."""
        from native_apply import PreparedApply
        if type(prepared) is not PreparedApply or prepared.issuer.operations is not self:
            raise Conflict('issued Apply cleanup authority required')
        prepared.check(fresh=False)
        if release:
            service=prepared.issuer.services.discover()
            if (prepared.issuer.recovery._pointer() is not None or service is None or service['phase']!='restored' or
                    service['operation']!=prepared.operation):raise Conflict('Apply cleanup participants unresolved')
        with self._locked() as (root,artifacts):
            state=self._load(root,artifacts);worker=state['workers'].get(prepared.operation)
            if worker is None or worker['phase']!='completed' or worker['owner']!=self.owner:
                raise Conflict('terminal Apply cleanup authority changed')
            record=state['records'][worker['record_id']]
            _require(record['identity']['execution_policy']=='native-apply-v1' and type(result) is dict and
                     result.get('operation')==prepared.operation and result.get('status') in ('SUCCESS','WARNING','ERROR'),'invalid Apply cleanup result')
            if release:
                if state['recovery']!={'record_id':record['id'],'identity_digest':record['identity_digest']}:
                    raise Conflict('Apply cleanup pin changed')
                state['recovery']=None
            worker['apply_result']=copy.deepcopy(result)
            self._commit(root,artifacts,state)
            return copy.deepcopy(result)

    def apply_abandoned(self,issuer,approval,*,capability=None):
        from native_disk import require_apply
        require_apply(capability,self,approval.operation,deleting=True)
        from native_apply import NativeApply,_NoEffectClose
        if type(issuer) is not NativeApply or issuer.operations is not self or type(approval) is not _NoEffectClose or issuer._abandon_issued.get(id(approval))!=(approval,approval.operation,approval.command,approval.revision,approval.participant,approval.pin):raise Conflict('issued no-effect abandonment required')
        with self._locked() as (root,artifacts):
            state=self._load(root,artifacts);self._cas(state,approval.revision)
            worker=state['workers'].get(approval.operation)
            if worker is None or not (worker['phase']=='accepted' or worker['phase'] in ('failed','recovery_required') and (worker.get('apply_result') or {}).get('no_effect') is True):raise Conflict('no-effect worker proof changed')
            record=state['records'][worker['record_id']]
            if record['identity']['execution_policy']!='native-apply-v1' or state['active'] not in (None,approval.operation) or state['recovery'] not in (None,{'record_id':record['id'],'identity_digest':record['identity_digest']}):raise Conflict('no-effect operation pin changed')
            append=worker['phase']!='failed'
            worker.update(phase='failed',outcome='failed',apply_result=dict(status='ERROR',operation=worker['id'],error='recovery-point-closed',close_command=approval.command,recovery_retained=False,no_effect=True))
            self._invalidate(record)
            if append:self._append_checkpoint(record,worker)
            state['recovery']=None
            if state['active']==worker['id']:state['active']=None
            self._commit(root,artifacts,state)
            return copy.deepcopy(worker['apply_result'])

    def apply_closed(self,issuer,command,*,capability=None):
        from native_disk import require_apply
        require_apply(capability,self,deleting=True)
        from native_apply import NativeApply
        if type(issuer) is not NativeApply or issuer.operations is not self:raise Conflict('explicit Apply close issuer required')
        receipt=issuer.recovery.closed_receipt(command);service=issuer.services.discover()
        require_apply(capability,self,receipt['operation'],deleting=True)
        if service is not None and (service['phase']!='restored' or service['operation']!=receipt['operation']):
            raise Conflict('reviewed close service restoration incomplete')
        with self._locked() as (root,artifacts):
            state=self._load(root,artifacts);worker=state['workers'].get(receipt['operation'])
            if worker is None:raise Conflict('reviewed close operation missing')
            record=state['records'][worker['record_id']]
            if ((worker.get('apply_result') or {}).get('close_command')==command):
                return copy.deepcopy(worker['apply_result'])
            healthy=worker['phase']=='completed' and (worker.get('apply_result') or {}).get('cleanup_debt') is True
            if (record['identity']['execution_policy']!='native-apply-v1' or
                    state['recovery']!={'record_id':record['id'],'identity_digest':record['identity_digest']} or
                    worker['phase'] not in ('accepted','effect_started','recovery_required') and not healthy):
                raise Conflict('reviewed close operation fence changed')
            if healthy:
                previous=worker['apply_result']
                worker['apply_result']=dict(status=previous['healthy_status'],operation=worker['id'],
                    residual=copy.deepcopy(previous.get('residual',[])),selected=[],close_command=command)
            else:
                worker.update(phase='failed',outcome='failed',apply_result=dict(status='ERROR',operation=worker['id'],
                    error='recovery-point-closed',close_command=command,recovery_retained=False))
                self._invalidate(record);self._append_checkpoint(record,worker)
            state['recovery']=None
            if state['active']==worker['id']:state['active']=None
            self._commit(root,artifacts,state)
            return copy.deepcopy(worker['apply_result'])

    @contextmanager
    def _locked(self, *, create=True):
        _require(getattr(self,'_scope_execution',None) is None,'scope execution owns canonical critical section')
        with self.mutex:
            descriptors = []
            try:
                fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
                descriptors.append(fd)
                for part in self.data_root.parts[1:]:
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    descriptors.append(fd)
                for part in ("native-operations", "artifacts"):
                    if create:
                        try:
                            os.mkdir(part, 0o700, dir_fd=fd)
                            os.fsync(fd)
                        except FileExistsError:
                            pass
                    fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    descriptors.append(fd)
                    _require(stat.S_IMODE(os.fstat(fd).st_mode) == 0o700, "unsafe directory permissions")
                root, artifacts = descriptors[-2:]
                lock = os.open("lock", os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=root)
                descriptors.append(lock)
                self._regular(lock)
                fcntl.flock(lock, fcntl.LOCK_EX if create else fcntl.LOCK_SH | fcntl.LOCK_NB)
                yield root, artifacts
            finally:
                for fd in reversed(descriptors):
                    os.close(fd)

    @staticmethod
    def _regular(fd):
        info = os.fstat(fd)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                 stat.S_IMODE(info.st_mode) == 0o600, "unsafe file")

    def _read_file(self, directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            self._regular(fd)
            data = bytearray()
            while True:
                chunk = os.read(fd, CHUNK_SIZE)
                if not chunk:
                    break
                data.extend(chunk)
            self._regular(fd)
            return bytes(data)
        finally:
            os.close(fd)

    @contextmanager
    def _verified_artifact(self, directory, key, size=None):
        _hash(key)
        fd = os.open(key + ".diff", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            self._regular(fd)
            fingerprint = hashlib.sha256()
            measured = 0
            while True:
                chunk = os.read(fd, CHUNK_SIZE)
                if not chunk:
                    break
                measured += len(chunk)
                fingerprint.update(chunk)
            self._regular(fd)
            _require(fingerprint.hexdigest() == key and (size is None or measured == size), "artifact mismatch")
            os.lseek(fd, 0, os.SEEK_SET)
            yield fd, measured
        finally:
            os.close(fd)

    def _artifact(self, directory, key, size=None):
        # Bytes convenience only; authority validation uses the streaming path.
        with self._verified_artifact(directory, key, size) as (fd, measured):
            data = bytearray()
            while True:
                chunk = os.read(fd, CHUNK_SIZE)
                if not chunk:
                    return bytes(data)
                data.extend(chunk)

    def _verify(self, directory, key, size=None):
        with self._verified_artifact(directory, key, size) as (_, measured):
            return measured

    @staticmethod
    def _reader_chunks(producer):
        while True:
            chunk = producer.read(CHUNK_SIZE)
            _require(type(chunk) is bytes, "invalid artifact chunk")
            if not chunk:
                return
            yield chunk

    def _produce_artifact(self, directory, producer):
        temporary = ".tmp-" + str(uuid.uuid4())
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        fingerprint, size = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as stream:
                chunks = self._reader_chunks(producer) if hasattr(producer, "read") else iter(producer)
                for supplied in chunks:
                    _require(type(supplied) is bytes, "invalid artifact chunk")
                    # A producer owns its supplied chunk; no second giant copy is made.
                    for offset in range(0, len(supplied), CHUNK_SIZE):
                        chunk = memoryview(supplied)[offset:offset + CHUNK_SIZE]
                        stream.write(chunk)
                        fingerprint.update(chunk)
                        size += len(chunk)
                stream.flush()
                self.fault("artifact_written")
                os.fsync(stream.fileno())
            self.fault("artifact_fsynced")
            key = fingerprint.hexdigest()
            try:
                self._verify(directory, key, size)
            except FileNotFoundError:
                os.replace(temporary, key + ".diff", src_dir_fd=directory, dst_dir_fd=directory)
                self.fault("artifact_replaced")
            # A matching name may be an orphan left before directory durability.
            # Adoption therefore has the same durability gate as a new rename.
            os.fsync(directory)
            self.fault("artifact_durable")
            return key, size
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def _write(self, directory, name, data, artifact=False):
        temporary = ".tmp-" + str(uuid.uuid4())
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                self.fault("artifact_written" if artifact else "state_written")
                os.fsync(stream.fileno())
            self.fault("artifact_fsynced" if artifact else "state_fsynced")
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            self.fault("artifact_replaced" if artifact else "state_replaced")
            os.fsync(directory)
            self.fault("artifact_durable" if artifact else "state_durable")
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def _validate_v1(self, state, artifacts):
        _fields(state, "schema revision instance_id updated_at app_version epoch records current workers active commands baseline_record_id baseline_scope active_scope")
        _require(type(state["schema"]) is int and state["schema"] == 1, "unsupported schema")
        _int(state["revision"])
        _int(state["epoch"])
        _id(state["instance_id"])
        _timestamp(state["updated_at"])
        _text(state["app_version"])
        _fields(state["current"], "migration scope save apply")
        for key in ("records", "workers", "commands"):
            _require(isinstance(state[key], dict), "invalid ledger")
        for key, record in state["records"].items():
            _id(key)
            _require(record["id"] == key, "record ID mismatch")
            _record(record, record["domain"])
            self._verify(artifacts, record["artifact"])
        for domain, key in state["current"].items():
            _require(key is None or key in state["records"] and state["records"][key]["domain"] == domain, "invalid current record")
        for key, worker in state["workers"].items():
            _id(key)
            _fields(worker, "id record_id owner epoch phase marker evidence outcome binding")
            _require(worker["id"] == key and worker["record_id"] in state["records"], "invalid worker reference")
            _id(worker["owner"])
            _id(worker["marker"])
            _hash(worker["binding"])
            _int(worker["epoch"])
            _require(0 < worker["epoch"] <= state["epoch"] and worker["phase"] in PHASES, "invalid worker")
            _require(worker["outcome"] is None or worker["outcome"] in {"completed", "failed", "invalidated"}, "invalid outcome")
            if worker["evidence"] is not None:
                _evidence(worker["evidence"])
                _require(worker["evidence"]["marker"] == worker["marker"], "wrong effect marker")
            record = state["records"][worker["record_id"]]
            expected_status = {"accepted": "consumed", "effect_started": "consumed",
                               "recovery_required": "recovery_required", "completed": "completed",
                               "failed": "stale", "invalidated": "stale"}[worker["phase"]]
            allowed_statuses = {expected_status, "cancelled"} if worker["phase"] in {"failed", "invalidated"} else {expected_status}
            _require(record["status"] in allowed_statuses, "worker/record status mismatch")
            terminal = worker["phase"] in {"completed", "failed", "invalidated"}
            _require(worker["outcome"] == (worker["phase"] if terminal else None), "outcome mismatch")
            if worker["phase"] == "completed":
                _require(worker["evidence"] == dict(record["identity"]["effect"], marker=worker["marker"]),
                         "completion evidence mismatch")
            else:
                _require(worker["evidence"] is None, "unexpected completion evidence")
            if record["approval"] is not None:
                _require(worker["binding"] == record["approval"]["binding"], "worker approval mismatch")
        for record in state["records"].values():
            linked = [worker for worker in state["workers"].values() if worker["record_id"] == record["id"]]
            _require(len(linked) <= 1, "reused candidate consent")
            if record["status"] in {"consumed", "recovery_required", "completed"}:
                _require(len(linked) == 1, "missing candidate worker")
            for checkpoint in record["checkpoints"]:
                worker = state["workers"].get(checkpoint["operation_id"])
                _require(worker is not None and worker["record_id"] == record["id"] and
                         0 < checkpoint["epoch"] <= worker["epoch"], "checkpoint reference mismatch")
                if checkpoint["phase"] == "completed":
                    _require(checkpoint["evidence"] == worker["evidence"] and worker["phase"] == "completed",
                             "checkpoint evidence mismatch")
                else:
                    _require(checkpoint["evidence"] is None, "unexpected checkpoint evidence")
            if linked:
                self._validate_history(record["checkpoints"], linked[0])
            else:
                _require(not record["checkpoints"], "unclaimed candidate history")
        active = state["active"]
        _require(active is None or active in state["workers"] and state["workers"][active]["phase"] in
                 {"accepted", "effect_started", "recovery_required"}, "invalid active worker")
        unresolved = {key for key, worker in state["workers"].items() if worker["phase"] in
                      {"accepted", "effect_started", "recovery_required"}}
        _require(unresolved == ({active} if active else set()), "multiple unresolved workers")
        epochs = {}
        for record in state["records"].values():
            for checkpoint in record["checkpoints"]:
                epoch = checkpoint["epoch"]
                operation = checkpoint["operation_id"]
                _require(epoch not in epochs or epochs[epoch] == operation, "claim epoch reused by another worker")
                epochs[epoch] = operation
        _require(len(epochs) == state["epoch"] and (not epochs or min(epochs) == 1 and max(epochs) == state["epoch"]),
                 "incomplete claim epoch history")
        if active:
            _require(state["workers"][active]["epoch"] == state["epoch"], "active worker is not current owner epoch")
        for key, command in state["commands"].items():
            _id(key)
            _fields(command, "payload_digest operation_id")
            _hash(command["payload_digest"])
            _require(command["operation_id"] in state["workers"], "invalid command reference")
            worker = state["workers"][command["operation_id"]]
            _require(command["payload_digest"] == digest({"record_id": worker["record_id"], "binding": worker["binding"]}),
                     "command payload mismatch")
        _require(len(state["commands"]) == len(state["workers"]) and
                 len({command["operation_id"] for command in state["commands"].values()}) == len(state["workers"]),
                 "command/worker graph mismatch")
        self._validate_scope_history(state)

    def _validate_v2(self, state, artifacts):
        _fields(state, "schema revision instance_id updated_at app_version epoch records current workers active commands baseline_record_id baseline_scope active_scope generation retention checkpoint anchors baseline_provenance recovery" + (" scope_pending" if "scope_pending" in state else "") + (" scope_rules_record_id" if "scope_rules_record_id" in state else ""))
        _validate_scope_pending(state)
        _require(type(state["schema"]) is int and state["schema"] == 2, "unsupported schema")
        _int(state["revision"])
        _int(state["epoch"])
        _id(state["instance_id"])
        _timestamp(state["updated_at"])
        _text(state["app_version"])
        _fields(state["current"], "migration scope save apply")
        for key in ("records", "workers", "commands"):
            _require(isinstance(state[key], dict), "invalid ledger")
        for key, record in state["records"].items():
            _id(key)
            _require(record["id"] == key, "record ID mismatch")
            _record(record, record["domain"], 2)
            self._verify(artifacts, record["artifact"], record["artifact_size"])
        for domain, key in state["current"].items():
            _require(key is None or key in state["records"] and state["records"][key]["domain"] == domain, "invalid current record")
        for key, worker in state["workers"].items():
            _id(key)
            _publication_worker_fields(worker,state['records'][worker['record_id']])
            _require(worker["id"] == key and worker["record_id"] in state["records"], "invalid worker reference")
            _id(worker["owner"])
            _id(worker["marker"])
            _hash(worker["binding"])
            _int(worker["epoch"])
            _require(0 < worker["epoch"] <= state["epoch"] and worker["phase"] in PHASES, "invalid worker")
            _require(worker["outcome"] is None or worker["outcome"] in {"completed", "failed", "invalidated"}, "invalid outcome")
            if worker["evidence"] is not None:
                _evidence(worker["evidence"])
                _require(worker["evidence"]["marker"] == worker["marker"], "wrong effect marker")
            record = state["records"][worker["record_id"]]
            if record['identity']['execution_policy']=='native-publication-v1':
                _require(worker['publication_cleanup_owner']['epoch']<=state['epoch'],'future cleanup authority')
            expected_status = {"accepted": "consumed", "effect_started": "consumed",
                               "recovery_required": "recovery_required", "completed": "completed",
                               "failed": "stale", "invalidated": "stale"}[worker["phase"]]
            allowed_statuses = {expected_status, "cancelled"} if worker["phase"] in {"failed", "invalidated"} else {expected_status}
            _require(record["status"] in allowed_statuses, "worker/record status mismatch")
            terminal = worker["phase"] in {"completed", "failed", "invalidated"}
            _require(worker["outcome"] == (worker["phase"] if terminal else None), "outcome mismatch")
            if worker["phase"] == "completed":
                _require(worker["evidence"] == dict(record["identity"]["effect"], marker=worker["marker"]),
                         "completion evidence mismatch")
            else:
                _require(worker["evidence"] is None or _publication_refused(record,worker), "unexpected completion evidence")
            if record["approval"] is not None:
                _require(worker["binding"] == record["approval"]["binding"], "worker approval mismatch")
        for record in state["records"].values():
            linked = [worker for worker in state["workers"].values() if worker["record_id"] == record["id"]]
            _require(len(linked) <= 1, "reused candidate consent")
            if record["status"] in {"consumed", "recovery_required", "completed"}:
                _require(len(linked) == 1, "missing candidate worker")
            for checkpoint in record["checkpoints"]:
                worker = state["workers"].get(checkpoint["operation_id"])
                _require(worker is not None and worker["record_id"] == record["id"] and
                         0 < checkpoint["epoch"] <= worker["epoch"], "checkpoint reference mismatch")
                if checkpoint["phase"] == "completed":
                    _require(checkpoint["evidence"] == worker["evidence"] and worker["phase"] == "completed",
                             "checkpoint evidence mismatch")
                elif checkpoint["phase"] == "failed" and _publication_refused(record,worker):
                    _require(checkpoint["evidence"] == worker["evidence"], "publication refusal checkpoint mismatch")
                else:
                    _require(checkpoint["evidence"] is None, "unexpected checkpoint evidence")
            if linked:
                self._validate_bounded_history(record, linked[0])
            else:
                _require(not record["checkpoints"], "unclaimed candidate history")
        active = state["active"]
        _require(active is None or active in state["workers"] and state["workers"][active]["phase"] in
                 {"accepted", "effect_started", "recovery_required"}, "invalid active worker")
        unresolved = {key for key, worker in state["workers"].items() if worker["phase"] in
                      {"accepted", "effect_started", "recovery_required"}}
        _require(unresolved == ({active} if active else set()), "multiple unresolved workers")
        _int(state["generation"])
        _int(state["retention"])
        generations = set()
        for record in state["records"].values():
            _require(record["instance_id"] == state["instance_id"] and record["generation"] <= state["generation"] and
                     record["generation"] not in generations, "candidate namespace mismatch")
            generations.add(record["generation"])
            if not record["checkpoints"]:
                _require(record["recovery_range"] is None, "unclaimed recovery range")
        recovery = state["recovery"]
        if recovery is not None:
            _fields(recovery, "record_id identity_digest")
            _require(recovery["record_id"] in state["records"] and
                     recovery["identity_digest"] == state["records"][recovery["record_id"]]["identity_digest"], "invalid recovery pin")
        if recovery is not None:
            retained=state['records'][recovery['record_id']]
            debt=next((w for w in state['workers'].values() if w['record_id']==retained['id']),None)
            if retained['identity']['execution_policy']=='native-publication-v1' and debt is not None and debt['phase'] in ('completed','failed'):
                _require(state['active'] is None and debt['publication_cleanup_owner']['epoch']==state['epoch'],'terminal cleanup lifecycle epoch mismatch')
        for debt in state['workers'].values():
            retained=state['records'][debt['record_id']]
            if retained['identity']['execution_policy']=='native-publication-v1' and debt['phase'] in ('completed','failed') and debt['publication_cleanup']<4:
                _require(recovery=={'record_id':retained['id'],'identity_digest':retained['identity_digest']},'native publication cleanup protection missing')
        operation_ids, legacy_ids, recovery_counts = set(), set(), {}
        for key, command in state["commands"].items():
            version=command.get('request_version')
            _fields(command, "command_id generation request_version payload_digest operation_id"+
                (' owner epoch pending_effect' if version==3 else ''))
            _id(command['command_id']); _int(command['generation']); _hash(command['payload_digest'])
            _require(type(version) is int and version in (1,2,3),'invalid request version')
            _require(command['operation_id'] in state['workers'],'invalid command reference')
            worker=state['workers'][command['operation_id']]; record=state['records'][worker['record_id']]
            _require(command['generation']==record['generation'] and key==self._command_key(command['generation'],command['command_id']),
                'command namespace mismatch')
            if version==3:
                _require(record['identity']['execution_policy']=='native-publication-v1','recovery command outside native profile')
                _id(command['owner']); _int(command['epoch'])
                _require(0<command['epoch']<=worker['publication_cleanup_owner']['epoch'] and command['pending_effect'] in ('reconcile','push','cleanup','abandon'),
                    'invalid native recovery receipt')
                _require(command['payload_digest']==_publication_command_payload(record,worker,command),'recovery command payload mismatch')
                bucket=(worker['id'],command['pending_effect']=='push')
                recovery_counts[bucket]=recovery_counts.get(bucket,0)+1
                _require(recovery_counts[bucket]<=(128 if bucket[1] else 16),'unbounded native recovery receipts')
            else:
                _require(worker['id'] not in operation_ids,'duplicate original command reference'); operation_ids.add(worker['id'])
                if version==1:
                    _require(record['binding_version']==1 and command['command_id'] not in legacy_ids,'native or duplicate receipt in legacy namespace')
                    legacy_ids.add(command['command_id'])
                _require(command['payload_digest']==self._payload(state,record,worker['binding'],version),'command payload mismatch')
        _require(operation_ids==set(state['workers']),'command/worker graph mismatch')
        self._validate_provenance(state)

    def _validate(self, state, artifacts):
        _require(isinstance(state, dict) and type(state.get("schema")) is int, "unsupported schema")
        if state["schema"] == 1:
            self._validate_v1(state, artifacts)
        else:
            self._validate_v2(state, artifacts)
        if artifacts is not None:self._validate_scope_content(state,artifacts)

    @staticmethod
    def _validate_history(history, worker, record=None, summary=None):
        # Current status cannot erase an intent retained in durable history.
        _require(bool(history) and history[0]["phase"] == "accepted", "missing acceptance checkpoint")
        native=record is not None and record['identity']['execution_policy']=='native-publication-v1'
        if native:
            _require(history[0]['publication_attempt']=='not-sent' and history[0]['publication_cleanup']==0,'missing no-send acceptance')
            original=None
            for point in history:
                outcome=point['publication_outcome']
                if original is not None:
                    _require(outcome==original,'historical publication proof changed')
                elif outcome is not None and outcome['kind']=='published':
                    _require(point['phase'] in ('effect_started','recovery_required') and point['publication_attempt']=='possible',
                        'publication proof without durable possible intent')
                    _require((point['owner'],point['epoch'])==(outcome['owner'],outcome['epoch']),
                        'first publication proof owner mismatch')
                    original=outcome
        for previous, current in zip(history, history[1:]):
            marker_transition=(native and previous['phase'] in ('effect_started','recovery_required') and current['phase']=='effect_started' and
                previous['publication_attempt']=='not-sent' and current['publication_attempt']=='possible' and
                current['epoch']==previous['epoch'] and current['evidence']==previous['evidence'])
            cleanup_transition=(native and previous['phase']==current['phase'] and current['phase'] in ('completed','failed') and
                current['epoch']==previous['epoch'] and current['publication_cleanup']==previous['publication_cleanup']+1 and
                current['evidence']==previous['evidence'] and current['publication_outcome']==previous['publication_outcome'])
            if native:
                _require(current['publication_cleanup']>=previous['publication_cleanup'] and
                    (current['publication_cleanup']==previous['publication_cleanup'] or cleanup_transition),'publication cleanup chronology changed')
                _require(previous['publication_attempt']!='possible' or current['publication_attempt']=='possible','publication attempt marker regressed')
                _require(previous['publication_attempt']==current['publication_attempt'] or
                    marker_transition or current['phase']=='effect_started','publication marker changed without effect intent')
            refusal = (record is not None and _publication_refused(record,worker) and
                       previous["phase"] in ("effect_started","recovery_required") and current["phase"] == "failed" and
                       (previous['phase']=='effect_started' or worker.get('publication_attempt')=='not-sent') and
                       current["epoch"] == previous["epoch"] and current["evidence"] == worker["evidence"])
            apply_close=(record is not None and record['identity']['execution_policy']=='native-apply-v1' and
                         (worker.get('apply_result') or {}).get('error')=='recovery-point-closed' and
                         previous['phase'] in ('effect_started','recovery_required') and current['phase']=='failed' and
                         previous['epoch']==current['epoch'] and current['evidence'] is None)
            no_effect_takeover=(record is not None and record['identity']['execution_policy']=='native-apply-v1' and (worker.get('apply_result') or {}).get('no_effect') is True and previous['phase']=='accepted' and current['phase']=='recovery_required' and current['epoch']==previous['epoch']+1)
            _require(no_effect_takeover or cleanup_transition or marker_transition or refusal or apply_close or current["phase"] in WORKER_TRANSITIONS.get(previous["phase"], set()),
                     "impossible checkpoint transition")
            if current["epoch"] == previous["epoch"]:
                _require(current["owner"] == previous["owner"], "owner changed without takeover")
                _require(not (previous["phase"] == current["phase"] == "recovery_required"),
                         "recovery takeover requires a new epoch")
            else:
                summarized=(summary is not None and previous['phase']==current['phase']=='recovery_required' and
                    previous['epoch']==summary['first_epoch'] and current['epoch']==summary['last_epoch'])
                _require((current["epoch"] == previous["epoch"] + 1 or summarized) and
                         (previous["phase"], current["phase"]) in {
                             ("accepted", "invalidated"), *(([("accepted","recovery_required")] if record is not None and record['identity']['execution_policy']=='native-apply-v1' and (worker.get('apply_result') or {}).get('no_effect') is True else [])), ("effect_started", "recovery_required"),
                             ("recovery_required", "recovery_required")}, "impossible takeover epoch")
        latest = history[-1]
        if native:
            _require(latest['publication_cleanup']==worker['publication_cleanup'],'worker/history cleanup mismatch')
            _require(latest['publication_attempt']==worker['publication_attempt'],'worker/history attempt mismatch')
            _require(latest['publication_outcome']==worker['publication_outcome'],'worker/history refusal mismatch')
        _require(all(latest[field] == worker[field] for field in ("phase", "epoch", "owner")),
                 "worker/history mismatch")

    @staticmethod
    def _validate_scope_history(state):
        records = list(state["records"].values())
        if not records:
            _require(all(state[key] is None for key in ("baseline_record_id", "baseline_scope", "active_scope")),
                     "scope without baseline provenance")
            return
        source = state["records"].get(state["baseline_record_id"])
        _require(source is not None, "missing baseline identity context")
        codec = identity_codec(source["identity"])
        _scope(state["baseline_scope"], codec)
        _scope(state["active_scope"], codec)
        _id(state["baseline_record_id"])
        source = state["records"].get(state["baseline_record_id"])
        _require(source is not None and source["identity"]["scope"] == state["baseline_scope"],
                 "baseline lacks retained candidate provenance")
        expected = state["baseline_scope"]
        policies = {expected["revision"]: expected}
        # Exclusive contiguous epoch ranges establish actual operation order.
        # Policy revisions describe effects, and cannot establish that order.
        workers = sorted(state["workers"].values(), key=lambda worker:
                         state["records"][worker["record_id"]]["checkpoints"][0]["epoch"])
        for worker in workers:
            record = state["records"][worker["record_id"]]
            _require(record["identity"]["scope"] == expected,
                     "worker scope does not follow prior confirmed publication")
            if _scope_published(record, worker):
                # The preceding validators require exact completion evidence.
                expected = record["identity"]["scope_transaction"]["new"]
                _require(expected["revision"] not in policies, "duplicate scope publication")
                policies[expected["revision"]] = expected
        for record in records:
            _require(identity_codec(record["identity"]) == codec, "incompatible policy codec")
            policy = record["identity"]["scope"]
            _require(policy == policies.get(policy["revision"]), "record lacks established policy provenance")
        _require(state["active_scope"] == expected, "active scope lacks confirmed publication")

    @staticmethod
    def _checkpoint(through_epoch=0, scope=None, publications=0, migration=None):
        result = {"through_epoch": through_epoch, "scope": copy.deepcopy(scope),
                  "publications": publications, "migration": copy.deepcopy(migration)}
        result["digest"] = digest(result)
        return result

    @staticmethod
    def _baseline(record):
        result = {key: copy.deepcopy(record[key]) for key in
                  ("id", "domain", "identity", "identity_digest", "artifact", "artifact_size")}
        result["digest"] = digest(result)
        return result

    @staticmethod
    def _capsule(record, worker):
        result = {"record": copy.deepcopy(record), "worker": copy.deepcopy(worker)}
        result["digest"] = digest(result)
        return result

    @staticmethod
    def _check_digest(value):
        _require(value["digest"] == digest({key: item for key, item in value.items() if key != "digest"}),
                 "checkpoint certificate mismatch")

    def _validate_capsule(self, value, domain):
        _fields(value, "record worker digest")
        self._check_digest(value)
        record, worker = value["record"], value["worker"]
        _record(record, domain, 2)
        _publication_worker_fields(worker,record)
        for key in ("id", "record_id", "owner", "marker"):
            _id(worker[key])
        _int(worker["epoch"])
        _require(worker["epoch"] > 0, "invalid certificate epoch")
        _require(worker["record_id"] == record["id"] and record["domain"] == domain and
                 record["status"] == "completed" and worker["phase"] == worker["outcome"] == "completed" and
                 worker["binding"] == record["approval"]["binding"], "invalid publication certificate")
        _evidence(worker["evidence"])
        _require(worker["evidence"] == dict(record["identity"]["effect"], marker=worker["marker"]),
                 "certificate completion evidence mismatch")
        for point in record["checkpoints"]:
            _require(point["operation_id"] == worker["id"] and 0 < point["epoch"] <= worker["epoch"] and
                     point["evidence"] == (worker["evidence"] if point["phase"] == "completed" else None),
                     "certificate checkpoint mismatch")
        self._validate_bounded_history(record, worker)
        return record, worker

    @staticmethod
    def _validate_bounded_history(record, worker):
        history, summary = record["checkpoints"], record["recovery_range"]
        if record['identity']['execution_policy']=='native-publication-v1':
            if summary is not None:
                _fields(summary,"first_epoch last_epoch count owner")
                for key in ('first_epoch','last_epoch','count'): _int(summary[key])
                _id(summary['owner'])
                recoveries=[p for p in history if p['phase']=='recovery_required']
                _require(len(recoveries)==2 and summary['last_epoch']>summary['first_epoch'] and
                    summary['count']==summary['last_epoch']-summary['first_epoch']+1 and
                    recoveries[0]['epoch']==summary['first_epoch'] and recoveries[-1]['epoch']==summary['last_epoch'] and
                    recoveries[-1]['owner']==summary['owner'],'invalid publication recovery range')
            NativeOperationStore._validate_history(history,worker,record,summary)
            _require(len(history)<=12,'unbounded publication history')
            return
        if summary is None:
            NativeOperationStore._validate_history(history, worker, record)
            _require(len(history) <= 6, "unbounded worker history")
            return
        _fields(summary, "first_epoch last_epoch count owner")
        for key in ("first_epoch", "last_epoch", "count"):
            _int(summary[key])
        _id(summary["owner"])
        _require(summary["last_epoch"] > summary["first_epoch"] and
                 summary["count"] == summary["last_epoch"] - summary["first_epoch"] + 1,
                 "invalid recovery range")
        recoveries = [point for point in history if point["phase"] == "recovery_required"]
        _require(len(recoveries) == 2 and len(history) in {4, 5}, "invalid summarized history")
        first, last = recoveries
        _require(first["epoch"] == summary["first_epoch"] and last["epoch"] == summary["last_epoch"] and
                 last["owner"] == summary["owner"], "recovery range mismatch")
        # Validate all retained legal transitions; the one erased range has a
        # typed recovery-only interval, never an arbitrary phase shortcut.
        before = history[:3]
        _require([point["phase"] for point in before] == ["accepted", "effect_started", "recovery_required"],
                 "missing summarized effect intent")
        NativeOperationStore._validate_history(before, dict(worker, **{key: first[key] for key in ("phase", "owner", "epoch")}))
        after = history[3:]
        NativeOperationStore._validate_history([dict(last, phase="accepted"),
                                               dict(last, phase="effect_started"), *after], worker)
        _require(all(point["operation_id"] == worker["id"] for point in history), "summary operation mismatch")

    def _maintain_anchors(self, state):
        if state["baseline_scope"] is None:
            return
        baseline = state["baseline_scope"]
        key = digest(baseline)
        if not state["anchors"]:
            state["anchors"][key] = {"policy": copy.deepcopy(baseline), "start_after_epoch": 0,
                                      "end_epoch": None, "kind": "baseline", "source": copy.deepcopy(state["baseline_provenance"])}
        for worker in sorted(state["workers"].values(), key=lambda item: item["epoch"]):
            record = state["records"][worker["record_id"]]
            if not _scope_published(record, worker) or worker["epoch"] <= state["checkpoint"]["through_epoch"]:
                continue
            old, new = (record["identity"]["scope_transaction"][part] for part in ("old", "new"))
            new_key = digest(new)
            if new_key not in state["anchors"]:
                old_anchor = state["anchors"].get(digest(old))
                _require(old_anchor is not None, "publication predecessor unavailable")
                old_anchor["end_epoch"] = _worker_end(worker)
                state["anchors"][new_key] = {"policy": copy.deepcopy(new), "start_after_epoch": _worker_end(worker),
                                             "end_epoch": None, "kind": "publication", "source": self._capsule(record, worker)}

    def _validate_provenance(self, state):
        checkpoint = state["checkpoint"]
        _fields(checkpoint, "through_epoch scope publications migration digest")
        self._check_digest(checkpoint)
        _int(checkpoint["through_epoch"])
        _int(checkpoint["publications"])
        frontier = checkpoint["through_epoch"]
        _require(frontier <= state["epoch"], "future compacted frontier")
        _require(isinstance(state["anchors"], dict), "invalid anchors")
        baseline = state["baseline_provenance"]
        if baseline is None:
            _require(state["epoch"] == state["generation"] == 0, "uncovered empty store high-water marks")
            _require(not state["records"] and not state["workers"] and not state["anchors"] and
                     state["baseline_record_id"] is None and state["baseline_scope"] is None and
                     state["active_scope"] is None and checkpoint == self._checkpoint(), "scope without baseline provenance")
            return
        _fields(baseline, "id domain identity identity_digest artifact artifact_size digest")
        self._check_digest(baseline)
        _id(baseline["id"])
        _hash(baseline["artifact"])
        _int(baseline["artifact_size"])
        validate_identity(baseline["identity"], baseline["domain"])
        _require(baseline["identity_digest"] == digest(baseline["identity"]) and
                 baseline["id"] == state["baseline_record_id"] and
                 baseline["identity"]["scope"] == state["baseline_scope"], "baseline provenance mismatch")
        if baseline["id"] in state["records"]:
            _require(self._baseline(state["records"][baseline["id"]]) == baseline, "changed retained baseline")
        codec = identity_codec(baseline["identity"])
        _scope(state["baseline_scope"], codec)
        _scope(checkpoint["scope"], codec)
        _scope(state["active_scope"], codec)
        _require(checkpoint["scope"]["revision"] == state["baseline_scope"]["revision"] + checkpoint["publications"],
                 "compacted publication count mismatch")
        ordered = []
        for key, anchor in state["anchors"].items():
            _fields(anchor, "policy start_after_epoch end_epoch kind source")
            _scope(anchor["policy"], codec)
            _require(key == digest(anchor["policy"]), "policy anchor mismatch")
            _int(anchor["start_after_epoch"])
            if anchor["end_epoch"] is not None:
                _int(anchor["end_epoch"])
                _require(anchor["start_after_epoch"] < anchor["end_epoch"] <= state["epoch"], "invalid policy interval")
            if anchor["kind"] == "baseline":
                _require(anchor["policy"] == state["baseline_scope"] and anchor["source"] == baseline and
                         anchor["start_after_epoch"] == 0, "invalid baseline anchor")
            else:
                _require(anchor["kind"] == "publication", "invalid anchor kind")
                record, worker = self._validate_capsule(anchor["source"], "scope")
                _require(identity_codec(record["identity"]) == codec, "incompatible anchor codec")
                _require(record["instance_id"] == state["instance_id"] and record["generation"] <= state["generation"] and
                         record["identity"]["scope_transaction"]["new"] == anchor["policy"] and
                         _worker_end(worker) == anchor["start_after_epoch"] <= state["epoch"], "invalid anchor publication")
            ordered.append(anchor)
        ordered.sort(key=lambda item: item["policy"]["revision"])
        for left, right in zip(ordered, ordered[1:]):
            _require(left["end_epoch"] is not None and left["end_epoch"] <= right["start_after_epoch"] and
                     left["policy"]["revision"] < right["policy"]["revision"], "overlapping policy anchors")
            if right["policy"]["revision"] == left["policy"]["revision"] + 1:
                _require(left["end_epoch"] == right["start_after_epoch"] and
                         right["source"]["record"]["identity"]["scope"] == left["policy"], "adjacent policy boundary mismatch")
        def anchor_for(policy):
            anchor = state["anchors"].get(digest(policy))
            _require(anchor is not None and anchor["policy"] == policy, "record lacks established policy provenance")
            return anchor
        boundary = anchor_for(checkpoint["scope"])
        _require(boundary["start_after_epoch"] <= frontier and
                 (boundary["end_epoch"] is None or frontier <= boundary["end_epoch"]), "checkpoint boundary outside policy")
        intervals, archived = [], []
        for worker in state["workers"].values():
            record = state["records"][worker["record_id"]]
            start, end = record["checkpoints"][0]["epoch"], _worker_end(worker)
            anchor = anchor_for(record["identity"]["scope"])
            _require(anchor["start_after_epoch"] < start <= end and
                     (anchor["end_epoch"] is None or end <= anchor["end_epoch"]), "worker outside policy interval")
            if end <= frontier:
                _require(worker["phase"] in {"completed", "failed", "invalidated"}, "archived unresolved worker")
                archived.append((start, end, worker))
            else:
                _require(start > frontier, "worker crosses frontier")
                intervals.append((start, end, worker))
            if _scope_published(record, worker):
                new_anchor = state["anchors"].get(digest(record["identity"]["scope_transaction"]["new"]))
                _require(anchor["end_epoch"] == end, "publisher policy does not close")
                if new_anchor is not None:
                    _require(new_anchor["start_after_epoch"] == end and new_anchor["source"] == self._capsule(record, worker),
                             "retained publication certificate mismatch")
        for before, after in zip(sorted(archived), sorted(archived)[1:]):
            _require(before[1] < after[0], "overlapping archived workers")
        expected, next_epoch = checkpoint["scope"], frontier + 1
        for start, end, worker in sorted(intervals):
            _require(start == next_epoch, "incomplete claim epoch history")
            next_epoch = end + 1
            record = state["records"][worker["record_id"]]
            _require(record["identity"]["scope"] == expected, "worker scope does not follow prior confirmed publication")
            if _scope_published(record, worker):
                expected = record["identity"]["scope_transaction"]["new"]
        _require(next_epoch == state["epoch"] + 1, "incomplete claim epoch history")
        active_anchor = anchor_for(state["active_scope"])
        _require(expected == state["active_scope"] and active_anchor["end_epoch"] is None, "active scope lacks confirmed publication")
        for record in state["records"].values():
            _require(identity_codec(record["identity"]) == codec, "incompatible baseline codec")
            anchor_for(record["identity"]["scope"])
        if state["active"]:
            _require(state["workers"][state["active"]]["epoch"] == state["epoch"], "active worker not current epoch")
        self._validate_migration_checkpoint(state)
        self._validate_capsule_sources(state)

    def _validate_capsule_sources(self, state):
        """All provenance shares the same available candidate/worker namespace."""
        capsules = [anchor["source"] for anchor in state["anchors"].values() if anchor["kind"] == "publication"]
        if state["checkpoint"]["migration"] is not None:
            capsules.append(state["checkpoint"]["migration"])
        records_by_id = dict(state["records"])
        records_by_generation = {record["generation"]: record for record in state["records"].values()}
        workers_by_id = dict(state["workers"])
        workers_by_record = {worker["record_id"]: worker for worker in state["workers"].values()}
        intervals = [(state["records"][worker["record_id"]]["checkpoints"][0]["epoch"], worker["epoch"],
                      state["records"][worker["record_id"]], worker) for worker in state["workers"].values()]
        baseline = state["baseline_provenance"]
        for capsule in capsules:
            record, worker = capsule["record"], capsule["worker"]
            _require(record["instance_id"] == state["instance_id"] and record["generation"] <= state["generation"],
                     "capsule namespace mismatch")
            for retained in (records_by_id.get(record["id"]), records_by_generation.get(record["generation"])):
                if retained is not None:
                    _require(retained == record and workers_by_record.get(record["id"]) == worker,
                             "capsule retained candidate/source mismatch")
            retained_worker = workers_by_id.get(worker["id"])
            if retained_worker is not None:
                _require(retained_worker == worker and records_by_id.get(worker["record_id"]) == record,
                         "capsule retained operation/source mismatch")
            if record["id"] == baseline["id"]:
                _require(self._baseline(record) == baseline, "capsule baseline source mismatch")
            records_by_id[record["id"]] = record
            records_by_generation[record["generation"]] = record
            workers_by_id[worker["id"]] = worker
            workers_by_record[record["id"]] = worker
            intervals.append((record["checkpoints"][0]["epoch"], _worker_end(worker), record, worker))
        intervals.sort(key=lambda interval: (interval[0], interval[1]))
        for left, right in zip(intervals, intervals[1:]):
            if left[1] >= right[0]:
                _require(left[2:] == right[2:], "capsule epoch/source overlap")

    def _validate_migration_checkpoint(self, state):
        checkpoint = state["checkpoint"]
        frontier, capsule = checkpoint["through_epoch"], checkpoint["migration"]
        folded = [worker for worker in state["workers"].values()
                  if worker["epoch"] <= frontier and worker["phase"] == "completed" and
                  state["records"][worker["record_id"]]["domain"] == "migration"]
        if capsule is None:
            _require(not folded, "missing retained migration provenance")
            return
        record, worker = self._validate_capsule(capsule, "migration")
        _require(identity_codec(record["identity"]) == identity_codec(state["baseline_provenance"]["identity"]), "incompatible migration codec")
        start, end = record["checkpoints"][0]["epoch"], _worker_end(worker)
        _require(record["instance_id"] == state["instance_id"] and record["generation"] <= state["generation"],
                 "migration namespace mismatch")
        _require(end <= frontier, "migration outside compacted prefix")
        if folded:
            latest = max(folded, key=lambda item: item["epoch"])
            _require(latest["epoch"] <= end, "missing latest retained migration provenance")
            if latest["epoch"] == end:
                _require(capsule == self._capsule(state["records"][latest["record_id"]], latest),
                         "latest migration certificate mismatch")
        # Prefix policy evidence remains checkable even when the full source
        # and its old policy anchor have deliberately retired.
        policy = record["identity"]["scope"]
        _require(state["baseline_scope"]["revision"] <= policy["revision"] <= checkpoint["scope"]["revision"],
                 "migration policy outside compacted prefix")
        for retained_anchor in state["anchors"].values():
            if retained_anchor["kind"] != "publication":
                continue
            source_record, source_worker = retained_anchor["source"]["record"], retained_anchor["source"]["worker"]
            old_policy = source_record["identity"]["scope"]
            if policy["revision"] == old_policy["revision"]:
                _require(policy == old_policy and end <= source_worker["epoch"], "migration publication policy mismatch")
        for confirmed in (state["baseline_scope"], checkpoint["scope"]):
            if policy["revision"] == confirmed["revision"]:
                _require(policy == confirmed, "migration policy mismatch")
        anchor = next((item for item in state["anchors"].values()
                       if item["policy"]["revision"] == policy["revision"]), None)
        if anchor is not None:
            _require(anchor["policy"] == policy and anchor["start_after_epoch"] < start and
                     (anchor["end_epoch"] is None or end <= anchor["end_epoch"]), "migration outside retained policy interval")

    @staticmethod
    def _pins(state):
        pins = {key for key in state["current"].values() if key is not None}
        if state["active"]:
            pins.add(state["workers"][state["active"]]["record_id"])
        if state["recovery"]:
            pins.add(state["recovery"]["record_id"])
        if state.get("scope_pending"):
            pins.add(state["scope_pending"]["record_id"])
        if state.get("scope_rules_record_id"):
            pins.add(state["scope_rules_record_id"])
        pins.update(w['record_id'] for w in state['workers'].values() if (w.get('apply_result') or {}).get('no_effect') is True and not (w.get('apply_result') or {}).get('close_command'))
        return pins

    def _compact_graph(self, state):
        checkpoint = state["checkpoint"]
        frontier = checkpoint["through_epoch"]
        suffix = sorted((worker for worker in state["workers"].values() if _worker_end(worker) > frontier),
                        key=lambda worker: state["records"][worker["record_id"]]["checkpoints"][0]["epoch"])
        terminal = [worker for worker in suffix if worker["phase"] in {"completed", "failed", "invalidated"} and
                    not _scope_activation_pending(state["records"][worker["record_id"]],worker)]
        folded = terminal[:max(0, len(terminal) - state["retention"])]
        boundary, count, migration = checkpoint["scope"], checkpoint["publications"], checkpoint["migration"]
        for worker in folded:
            record = state["records"][worker["record_id"]]
            frontier = _worker_end(worker)
            if worker["phase"] == "completed":
                if _scope_published(record, worker):
                    boundary = record["identity"]["scope_transaction"]["new"]
                    count += 1
                elif record["domain"] == "migration":
                    migration = self._capsule(record, worker)
        state["checkpoint"] = self._checkpoint(frontier, boundary, count, migration)
        keep = self._pins(state) | {worker["record_id"] for worker in suffix if _worker_end(worker) > frontier}
        state["records"] = {key: record for key, record in state["records"].items() if key in keep}
        state["workers"] = {key: worker for key, worker in state["workers"].items() if worker["record_id"] in keep}
        state["commands"] = {key: receipt for key, receipt in state["commands"].items() if receipt["operation_id"] in state["workers"]}
        policies = {digest(record["identity"]["scope"]) for record in state["records"].values()}
        if boundary is not None:
            policies.update((digest(boundary), digest(state["active_scope"])))
        state["anchors"] = {key: anchor for key, anchor in state["anchors"].items() if key in policies}
        _require(len(state["records"]) <= state["retention"] + 6 and len(state["anchors"]) <= state["retention"] + 8,
                 "retention partition exceeds bound")

    def _validate_retention(self, state):
        frontier = state["checkpoint"]["through_epoch"]
        suffix = [worker for worker in state["workers"].values() if _worker_end(worker) > frontier]
        keep = self._pins(state) | {worker["record_id"] for worker in suffix}
        _require(set(state["records"]) == keep and
                 sum(worker["phase"] in {"completed", "failed", "invalidated"} and
                     not _scope_activation_pending(state["records"][worker["record_id"]],worker) for worker in suffix) <= state["retention"] and
                 len(state["records"]) <= state["retention"] + 8, "invalid retention partition")
        policies = {digest(record["identity"]["scope"]) for record in state["records"].values()}
        if state["baseline_scope"] is not None:
            policies.update((digest(state["checkpoint"]["scope"]), digest(state["active_scope"])))
        _require(set(state["anchors"]) == policies and len(policies) <= state["retention"] + 10,
                 "invalid retained anchor set")

    def _sweep(self, directory, state, manifests=False):
        marked = {record["artifact"] + ".diff" for record in state["records"].values()}
        if not manifests: marked.update(ref.key+".blob" for ref in self._scope_refs(state,directory))
        self.cleanup_errors = [] if not manifests else self.cleanup_errors
        try:
            names = os.listdir(directory)
        except OSError as exc:
            self.cleanup_errors.append(str(exc))
            return
        for name in names:
            temporary = re.fullmatch(r"\.tmp-([0-9a-f-]{36})", name)
            owned_temp = False
            if temporary:
                try:
                    _id(temporary[1])
                    owned_temp = True
                except InvalidState:
                    pass
            owned_artifact = not manifests and name.endswith((".diff",".blob")) and DIGEST.fullmatch(name[:-5])
            if not (owned_temp or owned_artifact) or name in marked:
                continue
            fd = None
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                self._regular(fd)
                before = os.fstat(fd)
                entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
                _require((before.st_dev, before.st_ino) == (entry.st_dev, entry.st_ino), "cleanup entry changed")
                # Faults/interleaved writers run before the final content proof;
                # timestamp equality alone cannot qualify same-size payloads.
                self.fault("cleanup_before_unlink")
                if name.endswith('.blob'):
                    fingerprint=hashlib.sha256()
                    while True:
                        chunk=os.read(fd,CHUNK_SIZE)
                        if not chunk:break
                        fingerprint.update(chunk)
                    _require(fingerprint.hexdigest()==name[:-5] and
                             _scope_content_identity(os.fstat(fd))==_scope_content_identity(before) and
                             _scope_content_identity(os.stat(name,dir_fd=directory,follow_symlinks=False))==_scope_content_identity(before),
                             'unowned or changed binary cleanup object')
                if name.endswith('.blob'):
                    _require(_scope_content_identity(os.fstat(fd))==_scope_content_identity(before) and
                             _scope_content_identity(os.stat(name,dir_fd=directory,follow_symlinks=False))==_scope_content_identity(before),
                             'binary cleanup descriptor or route changed')
                os.unlink(name, dir_fd=directory)
                self.fault("cleanup_unlinked")
            except (OSError, InvalidState) as exc:
                self.cleanup_errors.append(str(exc))
            finally:
                if fd is not None:
                    os.close(fd)
        try:
            os.fsync(directory)
            self.fault("cleanup_durable")
        except OSError as exc:
            self.cleanup_errors.append(str(exc))

    def compact(self, expected_revision,*,capability=None):
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            self._apply_state_gate(state,capability)
            self._commit(root, artifacts, state)
            return self._projection(state)

    def _apply_state_gate(self,state,capability):
        # Schema 1 has no native Apply profile/recovery slot. Existing legacy
        # replay and explicit upgrade checks retain sole authority for it.
        if state['schema']==1:return
        selected=set()
        if state['active']:selected.add(state['workers'][state['active']]['record_id'])
        if state['recovery']:selected.add(state['recovery']['record_id'])
        from native_disk import require_apply
        for record_id in selected:
            if state['records'][record_id]['identity']['execution_policy']=='native-apply-v1':
                worker=next(w for w in state['workers'].values() if w['record_id']==record_id)
                require_apply(capability,self,worker['id'])

    def recovery_pin(self, expected_revision, expected_slot, record_id=None, identity_digest=None,*,capability=None):
        """Exact CAS protection only; no recovery journal or rollback is performed."""
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            if state['active'] is not None and state['records'][state['workers'][state['active']]['record_id']]['identity']['execution_policy']=='native-publication-v1':
                raise Conflict('native publication protection requires issued authority')
            for candidate in (record_id,(state['recovery'] or {}).get('record_id'),state['workers'][state['active']]['record_id'] if state['active'] else None):
                if candidate is not None:self._apply_gate(state['records'][candidate]['identity'],capability)
            protected=state['recovery']
            if protected is not None and state['records'][protected['record_id']]['identity']['execution_policy']=='native-publication-v1':
                raise Conflict('native publication protection requires issued cleanup')
            if record_id is not None and record_id in state['records'] and state['records'][record_id]['identity']['execution_policy']=='native-publication-v1':
                raise Conflict('native publication protection requires issued authority')
            if state["recovery"] != expected_slot:
                raise Conflict("recovery slot changed")
            if record_id is None:
                _require(identity_digest is None, "invalid closing identity")
                state["recovery"] = None
            else:
                record = state["records"].get(record_id)
                if record is None or record["identity_digest"] != identity_digest:
                    raise Conflict("recovery record identity changed")
                state["recovery"] = {"record_id": record_id, "identity_digest": identity_digest}
            self._commit(root, artifacts, state)
            return copy.deepcopy(state["recovery"])

    def copy_diff(self, record_id, destination):
        """Verify to EOF before emitting bytes; hold the cooperative writer lock."""
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            record = state["records"][record_id]
            with self._verified_artifact(artifacts, record["artifact"], record.get("artifact_size")) as (fd, size):
                while True:
                    chunk = os.read(fd, CHUNK_SIZE)
                    if not chunk:
                        return size
                    view = memoryview(chunk)
                    while view:
                        written = destination.write(view)
                        _require(type(written) is int and 0 < written <= len(view), "invalid artifact consumer write")
                        view = view[written:]

    def accounting(self):
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            live = {record["artifact"]+".diff" for record in state["records"].values()}
            live.update(ref.key+".blob" for ref in self._scope_refs(state,artifacts))
            stored = reclaimable = temporary = 0
            for directory in (root, artifacts):
                for name in os.listdir(directory):
                    if not (re.fullmatch(r"[0-9a-f]{64}\.(?:diff|blob)", name) or re.fullmatch(r"\.tmp-[0-9a-f-]{36}", name)):
                        continue
                    fd = None
                    try:
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                        self._regular(fd)
                        size = os.fstat(fd).st_size
                        if name.startswith(".tmp-"):
                            temporary += size
                        else:
                            stored += size
                            if name not in live:
                                reclaimable += size
                    except (OSError, InvalidState):
                        pass
                    finally:
                        if fd is not None:
                            os.close(fd)
            return {"artifact_bytes": stored, "manifest_bytes": len(canonical(state)),
                    "temporary_bytes": temporary, "reclaimable_bytes": reclaimable}

    def upgrade(self, expected_revision):
        """Explicit CAS conversion; validate the entire original graph first."""
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            if state["schema"] == SCHEMA:
                return self._projection(state)
            state.update(schema=SCHEMA, generation=0, retention=self.retention, recovery=None,
                         anchors={}, baseline_provenance=None, checkpoint=self._checkpoint(scope=state["baseline_scope"]))
            for number, record in enumerate(sorted(state["records"].values(), key=lambda item: (item["created_at"], item["id"])), 1):
                record.update(instance_id=state["instance_id"], generation=number, binding_version=1,
                              artifact_size=self._verify(artifacts, record["artifact"]), recovery_range=None)
                state["generation"] = number
                linked = next((worker for worker in state["workers"].values() if worker["record_id"] == record["id"]), None)
                if linked:
                    history = record["checkpoints"]
                    record["checkpoints"] = []
                    for point in history:
                        self._append_checkpoint(record, dict(linked, **{key: point[key] for key in ("phase", "owner", "epoch", "evidence")}))
            if state["baseline_record_id"]:
                state["baseline_provenance"] = self._baseline(state["records"][state["baseline_record_id"]])
            commands = {}
            for command_id, receipt in state["commands"].items():
                record = state["records"][state["workers"][receipt["operation_id"]]["record_id"]]
                commands[self._command_key(record["generation"], command_id)] = dict(receipt, command_id=command_id,
                                                                                   generation=record["generation"], request_version=1)
            state["commands"] = commands
            self._commit(root, artifacts, state)
            return self._projection(state)

    def _initial_state(self):
        return {"schema": SCHEMA, "revision": 0, "instance_id": str(uuid.uuid4()),
                 "updated_at": _now(), "app_version": "unactivated", "epoch": 0,
                 "records": {}, "current": dict.fromkeys(sorted(DOMAINS)), "workers": {},
                 "active": None, "commands": {}, "baseline_record_id": None,
                 "baseline_scope": None, "active_scope": None,
                 "generation": 0, "retention": self.retention, "checkpoint": self._checkpoint(),
                 "anchors": {}, "baseline_provenance": None, "recovery": None}

    def _load(self, root, artifacts, *, create=True):
        try:
            state = _parse(self._read_file(root, "state.json"))
        except FileNotFoundError:
            if not create:
                raise
            state = self._initial_state()
            self._write(root, "state.json", canonical(state))
        try:
            self._validate(state, artifacts)
            if state["schema"] == 2:
                self._validate_retention(state)
        except (KeyError, TypeError, RecursionError) as exc:
            raise InvalidState("invalid native state graph") from exc
        return state

    def _commit(self, root, artifacts, state):
        state["revision"] += 1
        state["updated_at"] = _now()
        _require(state["schema"] == SCHEMA, "explicit schema upgrade required")
        self._maintain_anchors(state)
        self._validate(state, artifacts)
        self._compact_graph(state)
        self._validate(state, artifacts)
        self._write(root, "state.json", canonical(state))
        self._sweep(artifacts, state)
        self._sweep(root, state, manifests=True)

    @staticmethod
    def _cas(state, expected):
        _int(expected)
        if state["revision"] != expected:
            raise Conflict("native state revision changed")

    def read(self, *, observe=False):
        if observe:
            with self._locked(create=False) as (root, artifacts):
                return self._load(root, artifacts, create=False)
        with self._locked() as (root, artifacts):
            return self._load(root, artifacts)

    def refresh(self, *, observe=False):
        """Diagnostic projection only: never initializes editable decisions."""
        state = self.read(observe=True) if observe else self.read()
        result={"revision": state["revision"], "candidates": {
            domain: None if key is None else {field: copy.deepcopy(state["records"][key][field])
                                              for field in ("id", "status", "artifact", "identity_digest", "checkpoints")}
            for domain, key in state["current"].items()},
            "worker": None if state["active"] is None else {
                field: state["workers"][state["active"]][field] for field in ("id", "phase", "epoch", "outcome")},
            "draft": {}}
        if result['worker'] is not None and 'apply_result' in state['workers'][state['active']]:
            result['worker']['result']=copy.deepcopy(state['workers'][state['active']]['apply_result'])
        return result

    def read_diff(self, record_id):
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            record = state["records"][record_id]
            return self._artifact(artifacts, record["artifact"], record.get("artifact_size"))

    def preview(self, expected_revision, domain, identity, diff,*,capability=None):
        validate_identity(identity, domain)
        _require(type(diff) is bytes, "invalid artifact bytes")
        return self.preview_stream(expected_revision, domain, identity, (diff,),capability=capability)

    def preview_stream(self, expected_revision, domain, identity, producer,*,capability=None):
        validate_identity(identity, domain)
        self._apply_gate(identity,capability)
        if identity['execution_policy']=='native-apply-v1':
            from native_disk import qualified_apply
            qualified_apply(capability,self)
        if identity['execution_policy'] in ('native-publication-v1','native-scope-v1','native-scope-v2'):
            raise Conflict('native publication requires issued preparation')
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._apply_state_gate(state,capability)
            self._cas(state, expected_revision)
            if state['recovery'] is not None and state['records'][state['recovery']['record_id']]['identity']['execution_policy']=='native-publication-v1':
                raise Conflict('native publication cleanup unresolved')
            if state["active"]:
                raise Conflict("native worker unresolved")
            if domain == "scope" and state.get("scope_pending"):
                raise Conflict("native scope transaction unresolved")
            if state["active_scope"] is not None and identity["scope"] != state["active_scope"]:
                raise Conflict("scope change requires completed publication")
            _require(state["schema"] == SCHEMA, "explicit schema upgrade required")
            key, size = self._produce_artifact(artifacts, producer)
            state["generation"] += 1
            record_id = str(uuid.uuid4())
            record = {"id": record_id, "domain": domain, "identity": copy.deepcopy(identity),
                      "identity_digest": digest(identity), "artifact": key, "status": "preview",
                      "decisions": {}, "approval": None, "created_at": _now(),
                      "validated_at": None, "checkpoints": [], "recovery_range": None,
                      "artifact_size": size, "binding_version": 2, "instance_id": state["instance_id"],
                      "generation": state["generation"]}
            previous = state["current"][domain]
            if previous:
                self._invalidate(state["records"][previous])
            state["records"][record_id] = record
            state["current"][domain] = record_id
            if state["baseline_record_id"] is None:
                # First durable Preview in any domain anchors the supplied policy
                # atomically with its immutable source record. Later cancellation,
                # refresh or restart never opens bootstrap authority again.
                state["baseline_record_id"] = record_id
                state["baseline_scope"] = copy.deepcopy(identity["scope"])
                state["active_scope"] = copy.deepcopy(identity["scope"])
                state["baseline_provenance"] = self._baseline(record)
                state["checkpoint"] = self._checkpoint(scope=identity["scope"])
            self._commit(root, artifacts, state)
            return copy.deepcopy(record)

    @staticmethod
    def _invalidate(record):
        if record["status"] not in {"completed", "cancelled"}:
            record.update(status="stale", decisions={}, approval=None, validated_at=None)

    @staticmethod
    def _matches(record, observation):
        if not isinstance(observation, dict) or set(observation) != {"available", "identity"} or observation["available"] is not True:
            return False
        try:
            validate_identity(observation["identity"], record["domain"])
        except (InvalidState, TypeError):
            return False
        return observation["identity"] == record["identity"]

    @staticmethod
    def _scope_matches(state, record):
        return state["active_scope"] is not None and record["identity"]["scope"] == state["active_scope"]

    def review(self, expected_revision, record_id, decisions, approval_id, observation,*,capability=None):
        _id(approval_id)
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._apply_state_gate(state,capability)
            self._cas(state, expected_revision)
            record = state["records"][record_id]
            self._apply_gate(record['identity'],capability)
            if state.get("scope_pending") and record["domain"] == "apply":
                raise Conflict("pending scope blocks Apply")
            if record['identity']['execution_policy'] in ('native-publication-v1','native-scope-v1','native-scope-v2') or state['recovery'] is not None and state['records'][state['recovery']['record_id']]['identity']['execution_policy']=='native-publication-v1':
                raise Conflict('native publication requires issued review')
            if state["active"] or state["current"][record["domain"]] != record_id or record["status"] != "preview":
                raise Conflict("fresh Preview required")
            if not self._matches(record, observation) or not self._scope_matches(state, record):
                self._invalidate(record)
                self._commit(root, artifacts, state)
                raise Conflict("source changed; fresh Preview required")
            _approved_decisions(decisions, record["identity"])
            record.update(status="reviewed", decisions=copy.deepcopy(decisions), validated_at=_now())
            record["approval"] = {"id": approval_id, "binding": _binding(record, decisions)}
            self._commit(root, artifacts, state)
            return copy.deepcopy(record)

    @staticmethod
    def _command_key(generation, command_id):
        return str(generation) + ":" + command_id

    @staticmethod
    def _payload(state, record, binding, request_version):
        payload = {"record_id": record["id"], "binding": binding}
        if request_version == 2:
            payload.update(instance_id=state["instance_id"], generation=record["generation"])
        return digest(payload)

    def claim(self, expected_revision, command_id, record_id, approval_binding, observation,
              *, instance_id=_OMITTED, generation=_OMITTED,capability=None):
        _id(command_id)
        _hash(approval_binding)
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._apply_state_gate(state,capability)
            if record_id in state['records']:
                self._apply_gate(state['records'][record_id]['identity'],capability)
            if state.get("scope_pending"):
                raise Conflict("pending scope requires an issued native operation")
            if record_id in state['records'] and state['records'][record_id]['identity']['execution_policy'] in ('native-publication-v1','native-scope-v1','native-scope-v2'):
                raise Conflict('native publication requires issued claim')
            if state.get('recovery') is not None and state['records'][state['recovery']['record_id']]['identity']['execution_policy']=='native-publication-v1':
                raise Conflict('native publication cleanup unresolved')
            legacy_payload = digest({"record_id": record_id, "binding": approval_binding})
            if state["schema"] == 1:
                if instance_id is not _OMITTED or generation is not _OMITTED:
                    raise Conflict("candidate namespace unavailable before schema upgrade")
                previous = state["commands"].get(command_id)
                if previous and previous["payload_digest"] == legacy_payload:
                    return False, copy.deepcopy(state["workers"][previous["operation_id"]])
                raise Conflict("explicit schema upgrade required")
            if instance_id is _OMITTED and generation is _OMITTED:
                previous = next((receipt for receipt in state["commands"].values()
                                 if receipt["request_version"] == 1 and receipt["command_id"] == command_id), None)
                if previous and previous["payload_digest"] == legacy_payload:
                    return False, copy.deepcopy(state["workers"][previous["operation_id"]])
                raise Conflict("candidate namespace unavailable or different payload")
            if instance_id != state["instance_id"] or type(generation) is not int or generation <= 0:
                raise Conflict("candidate namespace unavailable")
            record = state["records"].get(record_id)
            if record is None or record["generation"] != generation:
                raise Conflict("candidate generation unavailable")
            key = self._command_key(generation, command_id)
            previous = state["commands"].get(key)
            payload = self._payload(state, record, approval_binding, previous["request_version"] if previous else 2)
            if previous:
                if previous["payload_digest"] != payload:
                    raise Conflict("command ID reused with different payload")
                return False, copy.deepcopy(state["workers"][previous["operation_id"]])
            self._cas(state, expected_revision)
            record = state["records"][record_id]
            if state.get("scope_pending") and record["domain"] == "apply":
                raise Conflict("pending scope blocks Apply")
            if state["active"] or record["status"] != "reviewed" or state["current"][record["domain"]] != record_id:
                raise Conflict("native authority unavailable")
            if record["approval"]["binding"] != approval_binding:
                raise Conflict("review binding changed")
            if not self._matches(record, observation) or not self._scope_matches(state, record):
                self._invalidate(record)
                self._commit(root, artifacts, state)
                raise Conflict("source changed; fresh Preview required")
            operation_id = (capability.operation if record['identity']['execution_policy']=='native-apply-v1'
                            else str(uuid.uuid4()))
            state["epoch"] += 1
            worker = {"id": operation_id, "record_id": record_id, "owner": self.owner,
                      "epoch": state["epoch"], "phase": "accepted", "marker": str(uuid.uuid4()),
                      "evidence": None, "outcome": None, "binding": approval_binding}
            if record['identity']['execution_policy']=='native-apply-v1':
                worker['apply_result']=None
            if record['identity']['execution_policy']=='native-publication-v1':
                worker['publication_attempt']='not-sent'
                worker['publication_outcome']=None
                worker['publication_confirmation']=None
                worker['publication_cleanup']=0
                worker['publication_cleanup_owner']={'owner':worker['owner'],'epoch':worker['epoch']}
                worker['publication_resources']=dict.fromkeys(PUBLICATION_RESOURCE_SLOTS,PUBLICATION_RESOURCE_EMPTY)
            record["status"] = "consumed"
            state["workers"][operation_id] = worker
            state["active"] = operation_id
            state["commands"][key] = {"command_id": command_id, "generation": generation, "request_version": 2,
                                       "payload_digest": payload, "operation_id": operation_id}
            self._append_checkpoint(record, worker)
            self._commit(root, artifacts, state)
            return True, copy.deepcopy(worker)

    def _owned(self, state, operation_id, epoch):
        _id(operation_id)
        _int(epoch)
        worker = state["workers"].get(operation_id)
        if not worker or state["active"] != operation_id or worker["owner"] != self.owner or worker["epoch"] != epoch:
            raise Conflict("native worker fenced")
        return worker, state["records"][worker["record_id"]]

    def checkpoint(self, expected_revision, operation_id, epoch, phase, observation=None, evidence=None, apply_result=None,*,capability=None):
        """Durable effect intent precedes future adapter calls; this makes none."""
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            worker, record = self._owned(state, operation_id, epoch)
            self._apply_gate(record['identity'],capability)
            if record['identity']['execution_policy']=='native-apply-v1':
                from native_disk import require_apply
                require_apply(capability,self,operation_id)
                if apply_result is not None:
                    _require(type(apply_result) is dict and apply_result.get('status') in ('SUCCESS','WARNING','ERROR') and
                             apply_result.get('operation')==operation_id,'invalid Apply result')
                    canonical(apply_result)
                    worker['apply_result']=copy.deepcopy(apply_result)
                _require(phase!='completed' or worker['apply_result'] is not None,'durable Apply result required')
            else:
                _require(apply_result is None,'Apply result outside native profile')
            if record['identity']['execution_policy'] in ('native-publication-v1','native-scope-v1','native-scope-v2'):
                raise Conflict('native publication or scope requires issued checkpoint')
            transitions = {key: phases - {"invalidated"} for key, phases in WORKER_TRANSITIONS.items()}
            transitions["recovery_required"] = {"completed"}
            if phase not in transitions.get(worker["phase"], set()):
                raise Conflict("invalid native worker transition")
            if phase == "effect_started":
                if not self._matches(record, observation) or not self._scope_matches(state, record):
                    self._invalidate(record)
                    worker.update(phase="invalidated", outcome="invalidated")
                    state["active"] = None
                    self._append_checkpoint(record, worker)
                    self._commit(root, artifacts, state)
                    raise Conflict("source changed; fresh Preview required")
            if phase == "completed":
                _evidence(evidence)
                expected = dict(record["identity"]["effect"], marker=worker["marker"])
                if evidence != expected:
                    raise Conflict("exact effect evidence required")
                worker["evidence"] = copy.deepcopy(evidence)
                record["status"] = "completed"
                if record["domain"] == "scope":
                    state["active_scope"] = copy.deepcopy(record["identity"]["scope_transaction"]["new"])
            elif phase == "recovery_required":
                record["status"] = "recovery_required"
            elif phase == "failed":
                self._invalidate(record)
            if record['identity']['execution_policy']=='native-publication-v1' and phase in ('effect_started','completed'):
                worker['publication_attempt']='possible'
            worker["phase"] = phase
            if phase in {"completed", "failed"}:
                worker["outcome"] = phase
                state["active"] = None
            self._append_checkpoint(record, worker)
            self._commit(root, artifacts, state)
            return copy.deepcopy(worker)

    def lifecycle(self, expected_revision, app_version, observations,*,capability=None):
        """Explicit restart/update takeover; never schedules or retries work."""
        observed=self.read(observe=True)
        selected=observed['active']
        if selected is None and observed['recovery'] is not None:
            selected=next((w['id'] for w in observed['workers'].values() if w['record_id']==observed['recovery']['record_id']),None)
        if selected is not None:
            worker=observed['workers'][selected];record=observed['records'][worker['record_id']]
            if record['identity']['execution_policy']=='native-apply-v1':
                from native_disk import require_apply,apply_transaction
                if capability is None:
                    from native_metadata import Binding
                    from native_inventory import Policy,linux_mounts
                    root=decode_native_path(record['identity']['root']['path'],absolute=True)
                    mounts=linux_mounts();binding=Binding(root,Policy(),mounts,tuple(m.id for m in mounts),True)
                    with apply_transaction(binding,(self,),selected,'close') as takeover:
                        return self.lifecycle(expected_revision,app_version,observations,capability=takeover)
                require_apply(capability,self,selected)
        _text(app_version)
        _require(isinstance(observations, dict), "invalid observations")
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            active = state["active"]
            active_record = state["workers"][active]["record_id"] if active else None
            for key in state["current"].values():
                if key and key != active_record:
                    record = state["records"][key]
                    if not self._matches(record, observations.get(key)) or not self._scope_matches(state, record):
                        self._invalidate(record)
            if active:
                worker = state["workers"][active]
                record = state["records"][worker["record_id"]]
                if record['identity']['execution_policy']=='native-apply-v1' and worker['phase']=='accepted':worker['apply_result']=dict(status='ERROR',operation=worker['id'],error='explicit-no-effect-cleanup-required',no_effect=True)
                state["epoch"] += 1
                worker.update(owner=self.owner, epoch=state["epoch"])
                if worker["phase"] == "accepted" and record['identity']['execution_policy']!='native-apply-v1':
                    self._invalidate(record)
                    worker.update(phase="invalidated", outcome="invalidated")
                    state["active"] = None
                else:
                    worker["phase"] = "recovery_required"
                    record["status"] = "recovery_required"
                if record['identity']['execution_policy']=='native-publication-v1':
                    worker['publication_cleanup_owner']={'owner':worker['owner'],'epoch':worker['epoch']}
                self._append_checkpoint(record, worker)
            elif state['recovery'] is not None:
                record=state['records'][state['recovery']['record_id']]
                terminal=next((w for w in state['workers'].values() if w['record_id']==record['id']),None)
                if record['identity']['execution_policy']=='native-publication-v1' and terminal is not None and terminal['phase'] in ('completed','failed'):
                    state['epoch']+=1
                    terminal['publication_cleanup_owner']={'owner':self.owner,'epoch':state['epoch']}
            if state.get("scope_pending") is not None:
                state["scope_pending"]["owner"]=self.owner
                state["scope_pending"]["authority_epoch"]+=1
            state["app_version"] = app_version
            self._commit(root, artifacts, state)
            return self._projection(state)

    @staticmethod
    def _append_checkpoint(record, worker):
        record["checkpoints"].append({"operation_id": worker["id"], "owner": worker["owner"],
                                      "epoch": worker["epoch"], "phase": worker["phase"],
                                      "evidence": copy.deepcopy(worker["evidence"])})
        if record['identity']['execution_policy']=='native-publication-v1':
            record['checkpoints'][-1]['publication_attempt']=worker['publication_attempt']
            record['checkpoints'][-1]['publication_outcome']=copy.deepcopy(worker['publication_outcome'])
            record['checkpoints'][-1]['publication_cleanup']=worker['publication_cleanup']
        if "recovery_range" in record:
            history = record["checkpoints"]
            recoveries = [point for point in history if point["phase"] == "recovery_required"]
            if len(recoveries) > 3 or record["recovery_range"] is not None:
                first, latest = recoveries[0], recoveries[-1]
                record["recovery_range"] = {"first_epoch": first["epoch"], "last_epoch": latest["epoch"],
                                            "count": latest["epoch"] - first["epoch"] + 1,
                                            "owner": latest["owner"]}
                insertion=next(i for i,p in enumerate(history) if p['phase']=='recovery_required')
                record["checkpoints"] = [point for point in history if point["phase"] != "recovery_required"]
                record["checkpoints"].insert(insertion, first)
                if latest is not first:
                    record["checkpoints"].insert(insertion+1, latest)

    @staticmethod
    def _projection(state):
        return {"revision": state["revision"], "active": state["active"], "app_version": state["app_version"]}

    def cancel(self, expected_revision, record_id,*,capability=None):
        with self._locked() as (root, artifacts):
            state = self._load(root, artifacts)
            self._cas(state, expected_revision)
            self._apply_state_gate(state,capability)
            record = state["records"][record_id]
            if state.get("scope_pending") and record["domain"] == "apply":
                raise Conflict("pending scope blocks Apply")
            if record['identity']['execution_policy'] in ('native-publication-v1','native-scope-v1','native-scope-v2') or state['recovery'] is not None and state['records'][state['recovery']['record_id']]['identity']['execution_policy']=='native-publication-v1':
                raise Conflict('native publication requires issued cancel')
            if state["active"] or record["status"] not in {"preview", "reviewed", "stale"}:
                raise Conflict("cannot cancel pending effect")
            record.update(status="cancelled", decisions={}, approval=None, validated_at=None)
            self._commit(root, artifacts, state)


PUBLICATION_RESOURCE_SLOTS=('commit','commit-packed','tree','tree-packed')
PUBLICATION_RESOURCE_EMPTY='0'*176


def _publication_resources(resources):
    _fields(resources,' '.join(PUBLICATION_RESOURCE_SLOTS))
    for token in resources.values():
        _require(type(token) is str and re.fullmatch('[0-9a-f]{176}',token) is not None,'invalid exact publication resource token')
        if token==PUBLICATION_RESOURCE_EMPTY: continue
        fields=tuple(int(token[i:i+16],16) for i in range(0,176,16))
        _require(stat.S_ISREG(fields[2]) and stat.S_IMODE(fields[2])==0o600 and fields[4]==1 and stat.S_ISDIR(fields[10]),'unsafe publication resource descriptor')


def _fill_publication_resources(value,prepared,resources):
    _publication_resources(resources)
    target=value['workers'][prepared.operation_id]['publication_resources']
    # Only the four fixed slots of this exact operation are accounting recipes.
    # Historical worker resources and every checkpoint remain untouched.
    initial=_parse(prepared.initial)['workers'].get(prepared.operation_id)
    for key in PUBLICATION_RESOURCE_SLOTS:
        original=PUBLICATION_RESOURCE_EMPTY if initial is None else initial['publication_resources'][key]
        _require(target[key]==original and (original==PUBLICATION_RESOURCE_EMPTY or resources[key]==original),'publication resource recipe transplant')
    value['workers'][prepared.operation_id]['publication_resources']=copy.deepcopy(resources)
    worker=value['workers'][prepared.operation_id];record=value['records'][prepared.record_id]
    if _scope_published(record,worker):
        anchor=value['anchors'].get(digest(record['identity']['scope_transaction']['new']))
        if anchor is not None:anchor['source']=NativeOperationStore._capsule(record,worker)


def _worker_end(worker):
    return worker['publication_cleanup_owner']['epoch'] if 'publication_cleanup_owner' in worker else worker['epoch']


def _publication_worker_fields(worker,record):
    names="id record_id owner epoch phase marker evidence outcome binding"
    if record['identity']['execution_policy']=='native-apply-v1':
        _fields(worker,names+' apply_result')
        result=worker['apply_result']
        _require(result is None or type(result) is dict and result.get('status') in ('SUCCESS','WARNING','ERROR') and
                 result.get('operation')==worker['id'],'invalid durable Apply result')
        return
    if record['identity']['execution_policy']=='native-publication-v1':
        _fields(worker,names+" publication_attempt publication_outcome publication_confirmation publication_cleanup publication_cleanup_owner publication_resources"+(" publication_scope_activated" if record["domain"]=="scope" else ""))
        if record["domain"]=="scope":
            _require(type(worker["publication_scope_activated"]) is bool and (not worker["publication_scope_activated"] or worker["phase"]=="completed" and worker["publication_cleanup"]==4), "invalid scope activation")
        _publication_resources(worker['publication_resources'])
        authority=worker['publication_cleanup_owner']
        _fields(authority,'owner epoch'); _id(authority['owner']); _int(authority['epoch'])
        _require(authority['epoch']>=worker['epoch'],'cleanup authority epoch regressed')
        _require(authority['epoch']!=worker['epoch'] or authority['owner']==worker['owner'],'cleanup owner changed without lifecycle epoch')
        _require(worker['phase'] in ('completed','failed') or authority=={'owner':worker['owner'],'epoch':worker['epoch']},'nonterminal cleanup ownership changed')
        _require(type(worker['publication_cleanup']) is int and 0<=worker['publication_cleanup']<=4,'invalid publication cleanup progress')
        _require(worker['publication_cleanup']==0 or worker['phase'] in ('completed','failed'),'nonterminal publication cleanup')
        if worker['publication_outcome'] is not None:
            _publication_outcome(worker['publication_outcome'],record,worker)
            if worker['publication_outcome']['kind']=='published':
                _require(worker['phase'] in ('effect_started','completed','recovery_required') and worker['publication_attempt']=='possible','wrong positive publication phase')
            else:
                _require(worker['phase']=='failed' and (worker['owner'],worker['epoch'])==
                    (worker['publication_outcome']['owner'],worker['publication_outcome']['epoch']),
                    'nonterminal or foreign publication refusal')
        if worker['publication_confirmation'] is not None:
            _publication_outcome(worker['publication_confirmation'],record,worker)
            _require(worker['publication_confirmation']['kind']=='published' and worker['publication_outcome'] is not None and
                worker['publication_outcome']['kind']=='published','confirmation without first publication proof')
        _require(worker['publication_outcome'] is None or worker['publication_outcome']['kind']!='published' or
            worker['publication_confirmation'] is not None,'missing current publication confirmation')
        _require(worker['publication_attempt'] in ('not-sent','possible'),'invalid publication attempt marker')
        _require(worker['phase']!='accepted' or worker['publication_attempt']=='not-sent','accepted publication was sent')
        _require(worker['phase']!='completed' or worker['publication_attempt']=='possible','completion without possible publication')
        _require(worker['phase']!='completed' or worker['publication_confirmation'] is not None and
            (worker['publication_confirmation']['owner'],worker['publication_confirmation']['epoch'])==(worker['owner'],worker['epoch']),'completion without current publication confirmation')
        for slot,token in worker['publication_resources'].items():
            _require(token==PUBLICATION_RESOURCE_EMPTY or worker['phase'] in ('completed','failed') and worker['publication_cleanup']>=(1 if slot.startswith('commit') else 3),'publication resource outside exact retirement stage')
        _require(worker['phase']!='completed' or worker['publication_outcome'] is not None and
            worker['publication_outcome']['kind']=='published','completion without persisted publication proof')
    else:
        _fields(worker,names)


class PreparedPublication:
    """Diagnostic exact bytes; only the preparing owner can execute the handle."""
    __slots__ = ('store', 'initial', 'artifact', 'record_id', 'operation_id', 'states',
                 'owner_id', 'namespace', '_initial_bytes', '_used', '_current', '_sealed', 'purpose')

    def __init__(self, store, initial, artifact, record_id, operation_id, states):
        self.store = store
        self.purpose="publication"
        self.initial = canonical(initial)
        self.artifact = bytes(artifact)
        self.record_id, self.operation_id = record_id, operation_id
        self.states = tuple((phase, canonical(value)) for phase, value in states.items())
        self.owner_id = store.owner
        self.namespace = (initial['instance_id'], initial['generation'] + 1)
        self._initial_bytes = None
        self._used, self._current = False, self.initial

    def __setattr__(self,name,value):
        if getattr(self,'_sealed',False):
            raise Conflict('immutable prepared publication')
        object.__setattr__(self,name,value)

    def _capture(self):
        return (self.initial,self.artifact,self.record_id,self.operation_id,self.states,
                self.owner_id,self.namespace,self._initial_bytes,self.purpose)

    def check(self):
        if (self.store._prepared_publication is not self or self.store.owner != self.owner_id or
                self._capture() != self.store._publication_snapshot or
                (self._used,self._current) != self.store._publication_progress):
            raise Conflict('foreign or expired prepared publication')
        return self

    def state(self, phase):
        return _parse(dict(self.states)[phase])

    def is_outcome_recipe(self,name):
        self.check()
        return name.startswith(('refused_','proved_','completed_','recovered_completed_','cleaned_','recovered_cleaned_','recovery_required_','pin-retirement-'))

    def qualify_payload(self,payload):
        self.check()
        if payload in tuple(raw for name,raw in self.states if not self.is_outcome_recipe(name)):
            return payload
        compiled=next((entry for entry in getattr(self.store,'_publication_compiled',()) if entry[:2]==(self,payload)),None)
        if compiled is None:
            raise Conflict('issuer compiled publication outcome required')
        name,template=next((name,raw) for name,raw in self.states if name==compiled[2])
        value=_parse(payload); worker=value['workers'][self.operation_id]; record=value['records'][self.record_id]
        _publication_outcome(worker['publication_outcome'],record,worker)
        expected=_parse(template)
        outcome=worker['publication_confirmation'] if worker['publication_outcome']['kind']=='published' else worker['publication_outcome']
        initial_worker=_parse(self.initial)['workers'].get(self.operation_id)
        _fill_publication_outcome(expected,self.operation_id,self.record_id,outcome,
            first=initial_worker is None or initial_worker['publication_outcome'] is None)
        _fill_publication_resources(expected,self,dict(compiled[3]))
        if canonical(expected)!=payload or len(payload)!=len(template):
            raise Conflict('compiled publication outcome recipe changed')
        return payload


def _fill_publication_outcome(value,operation_id,record_id,outcome,*,first):
    worker=value['workers'][operation_id]; record=value['records'][record_id]
    _publication_outcome(outcome,record,worker)
    slots=worker['publication_confirmation'] if outcome['kind']=='published' else worker['publication_outcome']
    _require(slots is not None and all(outcome[field]==slots[field] for field in ('kind','mode','owner','epoch','request')),
        'fixed publication outcome recipe binding changed')
    previous=worker['publication_outcome']
    if outcome['kind']=='published': worker['publication_confirmation']=copy.deepcopy(outcome)
    if first or outcome['kind']!='published': worker['publication_outcome']=copy.deepcopy(outcome)
    if outcome['kind']!='published': worker['evidence']=publication_refusal_evidence(record,worker,outcome['kind'])
    for point in record['checkpoints']:
        if first and point['publication_outcome']==previous and previous is not None:
            point['publication_outcome']=copy.deepcopy(outcome)
            if point['phase']=='failed': point['evidence']=copy.deepcopy(worker['evidence'])
    if _scope_published(record,worker):
        anchor=value['anchors'].get(digest(record['identity']['scope_transaction']['new']))
        if anchor is not None:anchor['source']=NativeOperationStore._capsule(record,worker)


def _compile_publication_outcome(store,prepared,name,outcome,*,proof=None):
    prepared.check()
    current=_parse(prepared._current)
    worker=current['workers'][prepared.operation_id]; record=current['records'][prepared.record_id]
    if name.startswith('proved_') or name in ('refused_known-not-sent','refused_no-update'):
        from native_publication import PublicationProof,KnownRefusal,NativePublication
        expected_type=PublicationProof if name.startswith('proved_') else KnownRefusal
        if type(proof) is not expected_type or type(proof.issuer) is not NativePublication or proof.issuer.store is not store:
            raise Conflict('issuer qualified outcome capability required by compiler')
        if name.startswith('proved_'):
            proof.issuer.qualify_publication_proof(proof,prepared)
            kind='published'; mode='original-push' if proof.outcome is not None else 'readback'
        else:
            kind=proof.issuer.qualify_refusal(proof,prepared); mode='original-push'
        expected={'kind':kind,'mode':mode,'owner':worker['owner'],'epoch':worker['epoch'],
            'tip':proof.observation.oid,'graph':proof.graph_digest,
            'transcript':None if proof.outcome is None else proof.outcome.transcript_digest,
            'request':publication_request_commitment(record,worker)}
        _require(proof.epoch==worker['epoch'] and outcome==expected,'qualified outcome capability payload changed')
    else:
        # Later lifecycle recipes inherit only the exact already issued current
        # confirmation. Shape-valid caller data is never a compilation grant.
        _require(proof is None and (worker['publication_confirmation'] if worker['publication_outcome']['kind']=='published' else worker['publication_outcome'])==outcome and
            (any(entry[0] is prepared and entry[1]==prepared._current for entry in getattr(store,'_publication_compiled',())) or
             _parse(prepared.initial)['workers'].get(prepared.operation_id,{}).get('publication_outcome')==worker['publication_outcome']),
            'retained issued outcome capability required by compiler')
    if not prepared.is_outcome_recipe(name): raise Conflict('fixed native outcome recipe required')
    template=dict(prepared.states)[name]
    value=_parse(template)
    initial_worker=_parse(prepared.initial)['workers'].get(prepared.operation_id)
    _fill_publication_outcome(value,prepared.operation_id,prepared.record_id,outcome,
        first=initial_worker is None or initial_worker['publication_outcome'] is None)
    _fill_publication_resources(value,prepared,worker['publication_resources'])
    payload=canonical(value)
    _require(len(payload)==len(template),'compiled publication outcome extent mismatch')
    _publication_graph(store,value,None,prepared.artifact,historical={
        r['artifact']:r['artifact_size'] for r in _parse(prepared.initial)['records'].values()})
    entry=(prepared,payload,name,tuple(value['workers'][prepared.operation_id]['publication_resources'].items()))
    previous=tuple(item for item in getattr(store,'_publication_compiled',()) if item[0] is prepared)
    if entry not in previous: previous+=(entry,)
    _require(len(previous)<=len(prepared.states),'unbounded compiled outcomes')
    store._publication_compiled=previous
    prepared.qualify_payload(payload)
    return payload


def _publication_command_payload(record,worker,receipt):
    return digest({'profile':'native-publication-recovery-command-v1','artifact':record['artifact'],
        'identity':record['identity_digest'],'operation':worker['id'],'owner':receipt['owner'],
        'epoch':receipt['epoch'],'pending_effect':receipt['pending_effect']})


def _publication_cleanup_recipes(store,states,terminal,operation_id,record_id,finalize,stem):
    previous=copy.deepcopy(terminal)
    for progress in range(previous['workers'][operation_id]['publication_cleanup']+1,5):
        value=copy.deepcopy(previous); worker=value['workers'][operation_id]; worker['publication_cleanup']=progress
        store._append_checkpoint(value['records'][record_id],worker)
        if not progress%2: value['revision']+=2 # reserve two exact resource writes; optional slots may be skipped
        finalize(value,stem+str(progress)); previous=value
        if progress%2:
            for slot in (('commit','commit-packed') if progress==1 else ('tree','tree-packed')):
                resource=copy.deepcopy(value)
                if slot.endswith('-packed'): resource['revision']+=1
                finalize(resource,stem+'resource-'+slot)
    # Reopen may start at an odd progress with its resource still unregistered.
    progress=terminal['workers'][operation_id]['publication_cleanup']
    if progress%2:
        for slot in (('commit','commit-packed') if progress==1 else ('tree','tree-packed')):
            resource=copy.deepcopy(terminal)
            if slot.endswith('-packed'): resource['revision']+=1
            finalize(resource,stem+'resource-'+slot)
    cleaned=copy.deepcopy(previous); cleaned['recovery']=None
    record=cleaned['records'][record_id]
    if record['domain']=='scope' and cleaned['workers'][operation_id]['phase']=='completed':
        cleaned['workers'][operation_id]['publication_scope_activated']=True
        cleaned['active_scope']=copy.deepcopy(record['identity']['scope_transaction']['new'])
        cleaned['scope_pending']=None
        cleaned['scope_rules_record_id']=record_id
    finalize(cleaned,stem+'finished')


def _publication_positive_recipes(store,states,initial,operation_id,record_id,finalize):
    worker=initial['workers'][operation_id]
    for mode in ('original-push','readback'):
        proved=copy.deepcopy(initial); w=proved['workers'][operation_id]; r=proved['records'][record_id]
        w['publication_confirmation']=_publication_outcome_template(r,w,'published',mode)
        if w['publication_outcome'] is None:
            w['publication_outcome']=copy.deepcopy(w['publication_confirmation'])
            r['checkpoints'][-1]['publication_outcome']=copy.deepcopy(w['publication_outcome'])
        finalize(proved,'proved_'+mode)
        completed=copy.deepcopy(proved); w=completed['workers'][operation_id]; r=completed['records'][record_id]
        w.update(phase='completed',outcome='completed',evidence=dict(r['identity']['effect'],marker=w['marker']))
        r['status']='completed'; completed['active']=None
        completed['recovery']={'record_id':record_id,'identity_digest':r['identity_digest']}
        store._append_checkpoint(r,w)
        prefix='recovered_completed_' if any(p['phase']=='recovery_required' for p in r['checkpoints']) else 'completed_'
        finalize(completed,prefix+mode)
        _publication_cleanup_recipes(store,states,completed,operation_id,record_id,finalize,prefix+mode+'_cleanup_')
        if worker['phase']=='effect_started':
            recovery=copy.deepcopy(proved); rw=recovery['workers'][operation_id]; rr=recovery['records'][record_id]
            rw['phase']='recovery_required'; rr['status']='recovery_required'
            store._append_checkpoint(rr,rw); finalize(recovery,'recovery_required_'+mode)


def _publication_empty(store, instance_id, timestamp):
    return {'schema': SCHEMA, 'revision': 0, 'instance_id': instance_id,
            'updated_at': timestamp, 'app_version': 'unactivated', 'epoch': 0,
            'records': {}, 'current': dict.fromkeys(sorted(DOMAINS)), 'workers': {},
            'active': None, 'commands': {}, 'baseline_record_id': None,
            'baseline_scope': None, 'active_scope': None, 'generation': 0,
            'retention': store.retention, 'checkpoint': store._checkpoint(),
            'anchors': {}, 'baseline_provenance': None, 'recovery': None}


@contextmanager
def _publication_observe(store, *, scope_execution=None):
    """No mkdir, lock creation, state initialization, takeover or artifact writes."""
    active=getattr(store,'_scope_execution',None)
    if active is not None and active[0]==threading.get_ident():
        _require(scope_execution is None,'nested scope execution forbidden')
        yield active[3],active[4]
        return
    descriptors = []
    try:
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(fd)
        for part in store.data_root.parts[1:]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        try:
            root = os.open('native-operations', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            yield None, None
            return
        descriptors.append(root)
        _require(stat.S_IMODE(os.fstat(root).st_mode) == 0o700, 'unsafe directory permissions')
        artifacts = os.open('artifacts', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
        descriptors.append(artifacts)
        _require(stat.S_IMODE(os.fstat(artifacts).st_mode) == 0o700, 'unsafe directory permissions')
        lock = os.open('lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        descriptors.append(lock)
        store._regular(lock)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Conflict('operation owner busy') from exc
        if scope_execution is not None:
            prepared,admission=scope_execution
            prepared.check()
            _require(admission.plan.evidence.scope_preparation is prepared and admission.active,'issued scope execution required')
            store._scope_execution=(threading.get_ident(),prepared,admission,root,artifacts,lock)
        try:yield root, artifacts
        finally:
            if scope_execution is not None:store._scope_execution=None
    finally:
        primary = __import__('sys').exc_info()[1]
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except BaseException as secondary:
                if primary is None:
                    primary = secondary
                else:
                    primary.cleanup_errors = tuple(getattr(primary, 'cleanup_errors', ())) + (
                        'publication-observation-close:fd=%d:%s' % (descriptor, secondary),)
        if primary is not None and __import__('sys').exc_info()[1] is None:
            raise primary


def _publication_graph(store, state, artifacts, artifact, *, historical=None):
    """Pure preparation graph check; disk admission later verifies historical bytes.

    The only prospective artifact exception is an independently hashed, exact
    measured immutable byte value. It is never an absent generic artifact pass.
    """
    key = hashlib.sha256(artifact).hexdigest()
    class Graph(NativeOperationStore):
        def _validate_scope_content(self,state,directory):
            pass  # Pure prospective graph; the admitted source recipe proves outputs.
        def _verify(self, directory, digest_key, size=None):
            if digest_key == key:
                _require(size == len(artifact), 'prospective artifact size mismatch')
                return len(artifact)
            if historical is not None:
                _require(digest_key in historical and size==historical[digest_key], 'historical compiled artifact binding changed')
                return historical[digest_key]
            _require(directory is not None, 'missing historical artifact directory')
            fd = os.open(digest_key + '.diff', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                self._regular(fd)
                measured = os.fstat(fd).st_size
                _require(size is None or measured == size, 'historical artifact size mismatch')
                return measured
            finally:
                os.close(fd)
    observer = Graph(store.data_root, owner=store.owner, retention=store.retention)
    observer._validate(state, artifacts)
    observer._validate_retention(state)


def _prepare_publication(store, expected_revision, identity, artifact_factory, decisions,
                         approval_id, command_id):
    domain=identity['direction']
    _require(domain in ('save','scope'), 'native publication domain required')
    validate_identity(identity, domain)
    _require(identity['execution_policy'] == 'native-publication-v1', 'publication profile required')
    _approved_decisions(decisions, identity)
    _id(approval_id); _id(command_id)
    with store.mutex, _publication_observe(store) as (root, artifacts):
        initial_bytes = store._read_file(root, 'state.json') if root is not None else None
        timestamp = _now()
        initial = _parse(initial_bytes) if initial_bytes is not None else _publication_empty(store, str(uuid.uuid4()), timestamp)
        store._cas(initial, expected_revision)
        _require(initial['schema'] == SCHEMA, 'explicit schema upgrade required')
        if any(receipt['command_id']==command_id for receipt in initial['commands'].values()):
            raise Conflict('publication command already recorded; diagnostics only')
        if initial['recovery'] is not None:
            raise Conflict('outstanding publication cleanup must finish first')
        if initial['active']:
            raise Conflict('native worker unresolved')
        if initial['active_scope'] is not None and (initial['active_scope'] != identity['scope'] or
                identity_codec(initial['baseline_provenance']['identity']) != identity_codec(identity)):
            raise Conflict('incompatible publication baseline')
        if initial.get('scope_pending') is not None:
            _require(initial['scope_pending']['owner']==store.owner,'canonical scope owner changed; explicit reopen required')
        if domain=='scope':
            pending=initial.get('scope_pending')
            _require(pending is not None and pending['old']==identity['scope'] and pending['new']==identity['scope_transaction']['new'], 'issued pending scope required')
        record_id, operation_id, marker = (str(uuid.uuid4()) for _ in range(3))
        namespace = {'instance_id': initial['instance_id'], 'generation': initial['generation'] + 1,
                     'record_id': record_id, 'operation_id': operation_id, 'owner': store.owner,
                     'epoch': initial['epoch'] + 1, 'marker': marker}
        artifact = artifact_factory(copy.deepcopy(namespace))
        _require(type(artifact) is bytes, 'exact publication artifact required')
        # The callback supplies the already reviewed envelope, not authority.
        from native_publication import validate_intent
        validate_intent(artifact, identity=identity, namespace=namespace)
        _publication_graph(store, initial, artifacts, artifact)
        state = copy.deepcopy(initial)
        state['generation'] += 1; state['epoch'] += 1
        record = {'id': record_id, 'domain': domain, 'identity': copy.deepcopy(identity),
                  'identity_digest': digest(identity), 'artifact': hashlib.sha256(artifact).hexdigest(),
                  'artifact_size': len(artifact), 'binding_version': 2,
                  'instance_id': state['instance_id'], 'generation': state['generation'],
                  'status': 'consumed', 'decisions': copy.deepcopy(decisions),
                  'approval': None, 'created_at': timestamp, 'validated_at': timestamp,
                  'checkpoints': [], 'recovery_range': None}
        record['approval'] = {'id': approval_id, 'binding': _binding(record, decisions)}
        previous = state['current'][domain]
        if previous: store._invalidate(state['records'][previous])
        state['records'][record_id] = record; state['current'][domain] = record_id
        if state['baseline_record_id'] is None:
            state['baseline_record_id'] = record_id
            state['baseline_scope'] = copy.deepcopy(identity['scope'])
            state['active_scope'] = copy.deepcopy(identity['scope'])
            state['baseline_provenance'] = store._baseline(record)
            state['checkpoint'] = store._checkpoint(scope=identity['scope'])
        worker = {'id': operation_id, 'record_id': record_id, 'owner': store.owner,
                  'epoch': state['epoch'], 'phase': 'accepted', 'marker': marker,
                  'evidence': None, 'outcome': None, 'binding': record['approval']['binding'],
                  'publication_attempt':'not-sent','publication_outcome':None,'publication_confirmation':None,'publication_cleanup':0,'publication_cleanup_owner':{'owner':store.owner,'epoch':state['epoch']},'publication_resources':dict.fromkeys(PUBLICATION_RESOURCE_SLOTS,PUBLICATION_RESOURCE_EMPTY)}
        if domain=='scope':worker['publication_scope_activated']=False
        state['workers'][operation_id] = worker; state['active'] = operation_id
        state['commands'][store._command_key(record['generation'], command_id)] = {
            'command_id': command_id, 'generation': record['generation'], 'request_version': 2,
            'payload_digest': store._payload(state, record, worker['binding'], 2), 'operation_id': operation_id}
        store._append_checkpoint(record, worker)
        states = {}
        def finalize(value, name):
            value['revision'] += 1; value['updated_at'] = timestamp
            store._maintain_anchors(value); store._compact_graph(value)
            _publication_graph(store, value, artifacts, artifact)
            states[name] = copy.deepcopy(value)
        finalize(state, 'accepted')
        effect = copy.deepcopy(state)
        effect['workers'][operation_id]['phase'] = 'effect_started'
        store._append_checkpoint(effect['records'][record_id], effect['workers'][operation_id])
        finalize(effect, 'effect_started')
        unsent_recovery=copy.deepcopy(effect)
        uw=unsent_recovery['workers'][operation_id]; ur=unsent_recovery['records'][record_id]
        uw['phase']='recovery_required'; ur['status']='recovery_required'
        store._append_checkpoint(ur,uw); finalize(unsent_recovery,'recovery_unsent')
        attempt=copy.deepcopy(effect); attempt['workers'][operation_id]['publication_attempt']='possible'
        store._append_checkpoint(attempt['records'][record_id],attempt['workers'][operation_id])
        finalize(attempt,'attempt_started')
        _publication_positive_recipes(store,states,attempt,operation_id,record_id,finalize)
        for phase in ('recovery_required',):
            value = copy.deepcopy(attempt); w = value['workers'][operation_id]; r = value['records'][record_id]
            w['phase'] = phase; r['status'] = phase
            if phase == 'completed':
                w['evidence'] = dict(identity['effect'], marker=marker)
                w['outcome'] = phase; value['active'] = None
                value['recovery']={'record_id':record_id,'identity_digest':record['identity_digest']}
            store._append_checkpoint(r, w); finalize(value, phase)
        for kind in ('known-not-sent','no-update'):
            refused=copy.deepcopy(effect if kind=='known-not-sent' else attempt); w=refused['workers'][operation_id]; r=refused['records'][record_id]
            w['publication_outcome']=_publication_outcome_template(r,w,kind)
            w.update(phase='failed',outcome='failed',evidence=publication_refusal_evidence(r,w,kind))
            store._invalidate(r); refused['active']=None
            refused['recovery']={'record_id':record_id,'identity_digest':r['identity_digest']}
            store._append_checkpoint(r,w); finalize(refused,'refused_'+kind)
            _publication_cleanup_recipes(store,states,refused,operation_id,record_id,finalize,'refused_'+kind+'_cleanup_')
        prepared = PreparedPublication(store, initial, artifact, record_id, operation_id, states)
        prepared._initial_bytes = initial_bytes
        store._prepared_publication = prepared
        store._publication_snapshot = prepared._capture()
        store._publication_progress = (False,prepared.initial)
        prepared._sealed = True
        return prepared


def _accept_publication(store, prepared, admission):
    prepared.check()
    admission.qualify_publication(prepared)
    if prepared._used:
        raise Conflict('prepared publication already consumed')
    with store._locked() as (root, artifacts):
        try: current = store._read_file(root, 'state.json')
        except FileNotFoundError: current = None
        if current != prepared._initial_bytes:
            raise Conflict('publication preparation became stale')
        object.__setattr__(prepared,"_used",True)
        store._publication_progress = (True,prepared._current)
        store._write(artifacts, hashlib.sha256(prepared.artifact).hexdigest() + '.diff', prepared.artifact, artifact=True)
        store._write(root, 'state.json', dict(prepared.states)['accepted'])
    object.__setattr__(prepared,'_current',dict(prepared.states)['accepted'])
    store._publication_progress = (prepared._used,prepared._current)
    admission.acknowledge_publication_owner(prepared, prepared._current)
    store.fault('publication-accepted')
    return prepared.state('accepted')['workers'][prepared.operation_id]


def _publication_checkpoint(store, prepared, admission, phase):
    prepared.check()
    admission.qualify_publication(prepared,failure_checkpoint=phase=='recovery_required')
    if phase not in ("effect_started","attempt_started","recovery_required","completed","recovered_completed"):
        raise Conflict("explicit scoped publication transition required")
    before=_parse(prepared._current)
    worker=before['workers'][prepared.operation_id]
    if phase in ('completed','recovered_completed'):
        confirmation=worker['publication_confirmation']
        _require(confirmation is not None and (confirmation['owner'],confirmation['epoch'])==(worker['owner'],worker['epoch']) and
            any(entry[0] is prepared and entry[1]==prepared._current and entry[2].startswith('proved_')
                for entry in getattr(store,'_publication_compiled',())),
            'current issued positive proof required before completion')
    name='recovery_unsent' if phase=='recovery_required' and worker['publication_attempt']=='not-sent' and 'recovery_unsent' in dict(prepared.states) else phase
    if phase in ('completed','recovered_completed','recovery_required') and worker['publication_confirmation'] is not None:
        name=phase+'_'+worker['publication_confirmation']['mode']
        next_bytes=_compile_publication_outcome(store,prepared,name,worker['publication_confirmation'])
    elif phase in ('completed','recovered_completed'):
        raise Conflict('positive publication proof must persist before completion')
    else:
        next_bytes = dict(prepared.states)[name]
    before, after = _parse(prepared._current), _parse(next_bytes)
    worker = before['workers'][prepared.operation_id]
    next_worker = after['workers'][prepared.operation_id]
    if (not prepared._used or before['active'] != prepared.operation_id or
            worker['owner'] != store.owner or next_worker['epoch'] != worker['epoch'] or
            (next_worker['phase'] not in WORKER_TRANSITIONS.get(worker['phase'], set()) and
             not (phase=='attempt_started' and worker['phase'] in ('effect_started','recovery_required') and next_worker['phase']=='effect_started' and
                  worker['publication_attempt']=='not-sent' and next_worker['publication_attempt']=='possible'))):
        raise Conflict('publication transition not prepared')
    with store._locked() as (root, artifacts):
        if store._read_file(root,'state.json') != prepared._current:
            raise Conflict('publication owner fenced')
        store._write(root,'state.json',next_bytes)
    object.__setattr__(prepared,"_current",next_bytes)
    store._publication_progress = (prepared._used,prepared._current)
    admission.acknowledge_publication_owner(prepared,next_bytes)
    store.fault('publication-' + next_worker['phase'])
    return copy.deepcopy(next_worker)


NativeOperationStore.prepare_publication = _prepare_publication
NativeOperationStore.accept_publication = _accept_publication
NativeOperationStore.publication_checkpoint = _publication_checkpoint


def _publication_refusal(store,prepared,admission,proof):
    prepared.check(); admission.qualify_publication(prepared)
    from native_publication import KnownRefusal,NativePublication
    if type(proof) is not KnownRefusal or type(proof.issuer) is not NativePublication or proof.issuer.store is not store or proof.issuer.saver.disk is not admission.disk:
        raise Conflict('exact publication refusal issuer required')
    kind=proof.outcome.kind
    name='refused_'+kind
    template=dict(prepared.states)[name]
    before=_parse(prepared._current)
    worker,record=store._owned(before,prepared.operation_id,proof.epoch)
    if (worker['phase'] not in ('effect_started','recovery_required','accepted') or
            worker['phase']=='recovery_required' and worker['publication_attempt']!='not-sent' or
            kind=='known-not-sent' and worker['publication_attempt']!='not-sent' or
            proof.epoch!=prepared.state('accepted')['workers'][prepared.operation_id]['epoch']):
        raise Conflict('only original known publication refusal may terminate')
    value=_parse(template); next_worker=value['workers'][prepared.operation_id]; next_record=value['records'][prepared.record_id]
    outcome={'kind':kind,'mode':'original-push','owner':worker['owner'],'epoch':worker['epoch'],
        'tip':proof.observation.oid,'graph':proof.graph_digest,
        'transcript':proof.outcome.transcript_digest,'request':publication_request_commitment(next_record,next_worker)}
    _publication_outcome(outcome,next_record,next_worker)
    next_bytes=_compile_publication_outcome(store,prepared,name,outcome,proof=proof)
    admission.qualify_publication_outcome(prepared,next_bytes)
    with store._locked() as (root,artifacts):
        if store._read_file(root,'state.json') != prepared._current: raise Conflict('publication owner fenced')
        store._write(root,'state.json',next_bytes)
    object.__setattr__(prepared,'_current',next_bytes)
    store._publication_progress=(prepared._used,prepared._current)
    admission.acknowledge_publication_owner(prepared,next_bytes)
    return _parse(next_bytes)['workers'][prepared.operation_id]


NativeOperationStore.publication_refusal = _publication_refusal


def _publication_proof(store,prepared,admission,proof):
    prepared.check(); admission.qualify_publication(prepared)
    from native_publication import PublicationProof,NativePublication
    if type(proof) is not PublicationProof or type(proof.issuer) is not NativePublication or proof.issuer.store is not store or proof.issuer.saver.disk is not admission.disk:
        raise Conflict('exact positive publication issuer required')
    before=_parse(prepared._current); worker,record=store._owned(before,prepared.operation_id,proof.epoch)
    if worker['phase'] not in ('effect_started','recovery_required') or worker['publication_attempt']!='possible':
        raise Conflict('positive publication requires current possible attempt')
    mode='original-push' if proof.outcome is not None else 'readback'
    outcome={'kind':'published','mode':mode,'owner':worker['owner'],'epoch':worker['epoch'],
        'tip':proof.observation.oid,'graph':proof.graph_digest,
        'transcript':None if proof.outcome is None else proof.outcome.transcript_digest,
        'request':publication_request_commitment(record,worker)}
    payload=_compile_publication_outcome(store,prepared,'proved_'+mode,outcome,proof=proof)
    admission.qualify_publication_outcome(prepared,payload)
    with store._locked() as (root,artifacts):
        if store._read_file(root,'state.json')!=prepared._current: raise Conflict('publication proof owner fenced')
        store._write(root,'state.json',payload)
    object.__setattr__(prepared,'_current',payload); store._publication_progress=(prepared._used,payload)
    admission.acknowledge_publication_owner(prepared,payload)
    store.fault('publication-proof')
    return outcome


NativeOperationStore.publication_proof=_publication_proof


def _publication_owner_write(store,prepared,admission,payload):
    prepared.qualify_payload(payload)
    if payload not in tuple(raw for name,raw in prepared.states if not prepared.is_outcome_recipe(name)):
        admission.qualify_publication_outcome(prepared,payload)
    else:
        admission.qualify_publication(prepared)
    with store._locked() as (root,artifacts):
        if store._read_file(root,'state.json')!=prepared._current: raise Conflict('publication owner fenced')
        store._write(root,'state.json',payload)
    object.__setattr__(prepared,'_current',payload); store._publication_progress=(prepared._used,payload)
    admission.acknowledge_publication_owner(prepared,payload)


def _publication_command(store,prepared,admission):
    prepared.check(); admission.qualify_publication(prepared)
    if prepared.purpose not in ('recovery','abandon'): raise Conflict('explicit recovery command required')
    before=_parse(prepared._current); after=_parse(dict(prepared.states)['recovery-command'])
    if before!=_parse(prepared.initial) or len(after['commands'])!=len(before['commands'])+1:
        raise Conflict('recovery command receipt already consumed')
    _publication_owner_write(store,prepared,admission,dict(prepared.states)['recovery-command'])
    store.fault('publication-recovery-command')


NativeOperationStore.publication_command=_publication_command


def _publication_cleanup_name(prepared,worker,suffix):
    initial_worker=_parse(prepared.initial)['workers'].get(prepared.operation_id)
    if prepared.purpose=='recovery' and initial_worker is not None and initial_worker['phase'] in ('completed','failed'):
        return 'pin-retirement-'+suffix
    outcome=worker['publication_outcome']
    if outcome['kind']=='published':
        prefix='recovered_completed' if any(p['phase']=='recovery_required' for p in
            _parse(prepared._current)['records'][prepared.record_id]['checkpoints']) else 'completed'
        return prefix+'_'+worker['publication_confirmation']['mode']+'_cleanup_'+suffix
    return 'refused_'+outcome['kind']+'_cleanup_'+suffix


def _publication_retirement(store,prepared,admission,progress,proof):
    prepared.check(); admission.qualify_publication(prepared)
    worker=_parse(prepared._current)['workers'][prepared.operation_id]
    if worker['phase'] not in ('completed','failed') or worker['publication_cleanup_owner']['owner']!=store.owner or progress!=worker['publication_cleanup']+1:
        raise Conflict('exact monotone terminal pin retirement required')
    from native_git import NativeGit, PinRetirement
    if type(proof) is not PinRetirement or type(proof.issuer) is not NativeGit or proof.issuer.root!=admission.disk.root:
        raise Conflict('issuer-qualified pin retirement required')
    proof.issuer.qualify_publication_retirement(proof,prepared,admission,progress)
    name=_publication_cleanup_name(prepared,worker,str(progress))
    outcome=worker['publication_confirmation'] if worker['publication_outcome']['kind']=='published' else worker['publication_outcome']
    payload=_compile_publication_outcome(store,prepared,name,outcome) if prepared.is_outcome_recipe(name) else dict(prepared.states)[name]
    _publication_owner_write(store,prepared,admission,payload)
    store.fault('publication-pin-retirement-'+str(progress))


NativeOperationStore.publication_retirement=_publication_retirement


def _publication_resource(store,prepared,admission,proof):
    prepared.check();admission.qualify_publication(prepared)
    from native_git import NativeGit, PublicationResource
    if type(proof) is not PublicationResource or type(proof.issuer) is not NativeGit or proof.issuer.root!=admission.disk.root:
        raise Conflict('issuer-qualified publication resource required')
    proof.issuer.qualify_publication_resource(proof,prepared,admission)
    current=_parse(prepared._current);worker=current['workers'][prepared.operation_id]
    _require(worker['publication_cleanup_owner']['owner']==store.owner and worker['publication_resources'][proof.slot]==PUBLICATION_RESOURCE_EMPTY,'publication resource already registered or foreign owner')
    name=_publication_cleanup_name(prepared,worker,'resource-'+proof.slot)
    template=dict(prepared.states)[name];value=_parse(template)
    outcome=worker['publication_confirmation'] if worker['publication_outcome']['kind']=='published' else worker['publication_outcome']
    initial_worker=_parse(prepared.initial)['workers'].get(prepared.operation_id)
    _fill_publication_outcome(value,prepared.operation_id,prepared.record_id,outcome,first=initial_worker is None or initial_worker['publication_outcome'] is None)
    resources=copy.deepcopy(worker['publication_resources']);resources[proof.slot]=proof.token
    _fill_publication_resources(value,prepared,resources)
    payload=canonical(value);_require(len(payload)==len(template),'publication resource extent changed')
    _publication_graph(store,value,None,prepared.artifact,historical={r['artifact']:r['artifact_size'] for r in _parse(prepared.initial)['records'].values()})
    entry=(prepared,payload,name,tuple(resources.items()))
    entries=tuple(item for item in getattr(store,'_publication_compiled',()) if item[0] is prepared)
    _require(len(entries)<len(prepared.states),'unbounded compiled resources')
    store._publication_compiled=entries+(entry,)
    _publication_owner_write(store,prepared,admission,payload)
    store.fault('publication-resource-'+proof.slot)


NativeOperationStore.publication_resource=_publication_resource


def _publication_cleanup(store,prepared,admission):
    prepared.check(); admission.qualify_publication(prepared)
    initial=_parse(prepared._current); worker=initial['workers'][prepared.operation_id]
    if worker['phase'] not in ('completed','failed') or worker['publication_cleanup']!=4 or initial['recovery']!={'record_id':prepared.record_id,
            'identity_digest':initial['records'][prepared.record_id]['identity_digest']}:
        raise Conflict('exact terminal publication cleanup required')
    name=_publication_cleanup_name(prepared,worker,'finished')
    outcome=worker['publication_confirmation'] if worker['publication_outcome']['kind']=='published' else worker['publication_outcome']
    payload=_compile_publication_outcome(store,prepared,name,outcome) if prepared.is_outcome_recipe(name) else dict(prepared.states)[name]
    _publication_owner_write(store,prepared,admission,payload)
    return _parse(payload)['workers'][prepared.operation_id]


NativeOperationStore.publication_cleanup = _publication_cleanup


def _prepare_publication_recovery(store,operation_id,expected_revision,*,command_id,pending_effect,abandon=False):
    """An explicit attempt prepares existing immutable intent, never new consent."""
    _id(operation_id)
    with store.mutex,_publication_observe(store) as (root,artifacts):
        if root is None: raise Conflict('publication owner store absent')
        raw=store._read_file(root,'state.json'); initial=_parse(raw)
        store._cas(initial,expected_revision)
        _id(command_id)
        if any(receipt['command_id']==command_id for receipt in initial['commands'].values()):
            raise Conflict('recovery command already recorded; diagnostics only')
        worker=initial['workers'].get(operation_id)
        authority=None if worker is None else (worker['publication_cleanup_owner'] if worker['phase'] in ('completed','failed') else {'owner':worker['owner'],'epoch':worker['epoch']})
        if authority is None or authority['owner']!=store.owner:
            raise Conflict('explicit lifecycle ownership required before recovery')
        record=initial['records'][worker['record_id']]
        if worker['phase'] in ('completed','failed') and initial['recovery']!={'record_id':record['id'],'identity_digest':record['identity_digest']}:
            raise Conflict('no pending terminal publication cleanup')
        if (record['identity']['execution_policy']!='native-publication-v1' or
                worker['phase'] not in ('accepted','effect_started','recovery_required','completed','failed')):
            raise Conflict('publication has no permitted pending recovery')
        artifact=store._read_file(artifacts,record['artifact']+'.diff')
        _require(hashlib.sha256(artifact).hexdigest()==record['artifact'] and len(artifact)==record['artifact_size'], 'exact retained intent required')
        _publication_graph(store,initial,artifacts,artifact)
        from native_publication import validate_intent
        intent=validate_intent(artifact)
        _require(intent.namespace['record_id']==record['id'] and intent.namespace['operation_id']==operation_id and
                 intent.namespace['instance_id']==initial['instance_id'] and intent.namespace['generation']==record['generation'], 'recovery intent namespace mismatch')
        if abandon and worker['publication_attempt']!='not-sent':
            raise Conflict('prior possible publication cannot be abandoned')
        original_initial=copy.deepcopy(initial)
        states={'accepted':copy.deepcopy(initial)}
        timestamp=_now()
        def finalize(value,name):
            value['revision']+=1; value['updated_at']=timestamp
            store._maintain_anchors(value); store._compact_graph(value)
            _publication_graph(store,value,artifacts,artifact)
            states[name]=copy.deepcopy(value)
        _require(sum(r['operation_id']==operation_id and r['request_version']==3 and (r['pending_effect']=='push')==(pending_effect=='push') for r in initial['commands'].values())<(128 if pending_effect=='push' else 16),
            'native recovery receipt capacity exhausted')
        command_state=copy.deepcopy(initial)
        receipt={'command_id':command_id,'generation':record['generation'],'request_version':3,
            'operation_id':operation_id,'owner':authority['owner'],'epoch':authority['epoch'],'pending_effect':pending_effect}
        receipt['payload_digest']=_publication_command_payload(record,worker,receipt)
        command_state['commands'][store._command_key(record['generation'],command_id)]=receipt
        finalize(command_state,'recovery-command')
        initial=command_state
        if worker['publication_attempt']=='not-sent' and worker['phase'] not in ('completed','failed'):
            refused=copy.deepcopy(initial); fw=refused['workers'][operation_id]; fr=refused['records'][record['id']]
            fw['publication_outcome']=_publication_outcome_template(fr,fw,'known-not-sent')
            fw.update(phase='failed',outcome='failed',evidence=publication_refusal_evidence(fr,fw,'known-not-sent'))
            store._invalidate(fr); refused['active']=None
            refused['recovery']={'record_id':record['id'],'identity_digest':fr['identity_digest']}
            store._append_checkpoint(fr,fw); finalize(refused,'refused_known-not-sent')
            _publication_cleanup_recipes(store,states,refused,operation_id,record['id'],finalize,'refused_known-not-sent_cleanup_')
            if worker['phase'] in ('effect_started','recovery_required'):
                attempt=copy.deepcopy(initial); attempt['workers'][operation_id].update(publication_attempt='possible',phase='effect_started')
                attempt['records'][record['id']]['status']='consumed'
                store._append_checkpoint(attempt['records'][record['id']],attempt['workers'][operation_id])
                finalize(attempt,'attempt_started')
        if not abandon and worker['phase'] not in ('completed','failed'):
            _publication_positive_recipes(store,states,states.get('attempt_started',initial),operation_id,record['id'],finalize)
            if worker['phase']=='effect_started':
                recovery=copy.deepcopy(states.get('attempt_started',initial)); w=recovery['workers'][operation_id]; r=recovery['records'][record['id']]
                w['phase']='recovery_required'; r['status']='recovery_required'
                store._append_checkpoint(r,w); finalize(recovery,'recovery_required')
        elif not abandon:
            _publication_cleanup_recipes(store,states,initial,operation_id,record['id'],finalize,'pin-retirement-')
        prepared=PreparedPublication(store,original_initial,artifact,record['id'],operation_id,states)
        prepared.purpose='abandon' if abandon else 'recovery'
        prepared.namespace=(initial['instance_id'],record['generation'])
        prepared._initial_bytes=raw; prepared._used=True
        store._prepared_publication=prepared; store._publication_snapshot=prepared._capture()
        store._publication_progress=(True,prepared._current); prepared._sealed=True
        return prepared


NativeOperationStore.prepare_publication_recovery = _prepare_publication_recovery


# Scope review storage is a separate issued owner recipe. Its artifact contains
# all candidate blobs, making durable review independent of Git object pruning.
def _scope_activation_pending(record,worker):
    return (record['domain']=='scope' and record['identity']['execution_policy']=='native-publication-v1' and
            worker['phase']=='completed' and not worker.get('publication_scope_activated',False))


def _scope_published(record, worker):
    if record['domain'] != 'scope' or worker['phase'] != 'completed':
        return False
    return (record['identity']['execution_policy'] != 'native-publication-v1' or
            worker.get('publication_scope_activated') is True and worker.get('publication_cleanup') == 4)


def _validate_scope_pending(state):
    rules=state.get('scope_rules_record_id')
    if rules is not None:
        _id(rules)
        record=state['records'].get(rules)
        worker=next((w for w in state['workers'].values() if w['record_id']==rules),None)
        _require(record is not None and worker is not None and _scope_published(record,worker) and
                 record['identity']['scope_transaction']['new']==state['active_scope'], 'active scope rules lack publication provenance')
    pending=state.get('scope_pending')
    if pending is None: return
    _fields(pending,'id record_id old new candidate_digest owner authority_epoch')
    _id(pending['owner']);_int(pending['authority_epoch'])
    _id(pending['id']); _id(pending['record_id']); _hash(pending['candidate_digest'])
    record=state['records'].get(pending['record_id'])
    _require(record is not None and record['domain']=='scope' and
             record['identity']['execution_policy'] in ('native-scope-v1','native-scope-v2') and
             record['status'] in ('preview','stale'), 'missing native scope provenance')
    current=state['records'].get(state['current']['scope'])
    _require(current is not None and (current['id']==record['id'] or
             current['identity']['execution_policy']=='native-publication-v1' and
             current['identity']['scope_transaction']==record['identity']['scope_transaction']), 'pending scope current provenance mismatch')
    _require(record['identity']['scope_transaction']==dict(old=pending['old'],new=pending['new']) and
             state['active_scope']==pending['old'] and
             record['identity']['effect']['after']==digest([pending['candidate_digest'],record['artifact']]), 'pending scope policy mismatch')


class PreparedScope:
    def __init__(self,store,initial,state,artifact,operation_id,payloads=()):
        self.store=store; self.initial=canonical(initial); self.state=canonical(state)
        self.artifact=artifact; self.operation_id=operation_id; self.owner_id=store.owner
        self.payloads=tuple(payloads)
        self.current=self.initial; self.used=False
        self.seal=(self.initial,self.state,self.artifact,self.operation_id,self.owner_id,self.payloads)

    def check(self):
        if (getattr(self.store,'_prepared_scope',None) is not self or self.store.owner!=self.owner_id or
                getattr(self.store,'_scope_snapshot',None)!=(self.initial,self.state,self.artifact,self.operation_id,self.owner_id,self.payloads) or
                (self.used,self.current)!=getattr(self.store,'_scope_progress',None)):
            raise Conflict('issued immutable scope preparation required')
        return self


def _prepare_scope(store,expected_revision,identity,artifact,candidate_digest,transaction_id=None,*,payloads=()):
    validate_identity(identity,'scope')
    _require(identity['execution_policy']=='native-scope-v2' and type(artifact) is bytes,'native scope profile required')
    with store.mutex,_publication_observe(store) as (root,artifacts):
        _require(root is not None,'established native baseline required')
        initial=_parse(store._read_file(root,'state.json'));store._validate(initial,artifacts)
        store._cas(initial,expected_revision)
        _require(initial['active_scope']==identity['scope'] and initial['active'] is None and initial['recovery'] is None,
                 'settled current baseline required')
        pending=initial.get('scope_pending')
        _require(pending is None or pending['id']==transaction_id,'different pending scope transaction')
        _require(pending is None or pending['owner']==store.owner,'canonical scope owner changed; explicit reopen required')
        state=copy.deepcopy(initial);record_id=str(uuid.uuid4());timestamp=_now()
        state['generation']+=1
        record=dict(id=record_id,domain='scope',identity=copy.deepcopy(identity),identity_digest=digest(identity),
                    artifact=hashlib.sha256(artifact).hexdigest(),artifact_size=len(artifact),binding_version=2,
                    instance_id=state['instance_id'],generation=state['generation'],status='preview',decisions={},
                    approval=None,created_at=timestamp,validated_at=None,checkpoints=[],recovery_range=None)
        previous=state['current']['scope']
        if previous:store._invalidate(state['records'][previous])
        state['records'][record_id]=record;state['current']['scope']=record_id
        state['scope_pending']=dict(id=transaction_id or str(uuid.uuid4()),record_id=record_id,
                    old=copy.deepcopy(identity['scope']),new=copy.deepcopy(identity['scope_transaction']['new']),candidate_digest=candidate_digest,
                    owner=store.owner,authority_epoch=0 if pending is None else pending['authority_epoch']+1)
        state['revision']+=1;state['updated_at']=timestamp
        store._maintain_anchors(state);store._compact_graph(state)
        _publication_graph(store,state,artifacts,artifact)
        prepared=PreparedScope(store,initial,state,artifact,str(uuid.uuid4()),payloads)
        store._prepared_scope=prepared;store._scope_snapshot=prepared.seal;store._scope_progress=(False,prepared.initial)
        return prepared


def _accept_scope(store,prepared,admission):
    prepared.check();admission.qualify_scope(prepared)
    _require(not prepared.used,'scope recipe already consumed')
    with _publication_observe(store) as (root,artifacts):
        _require(store._read_file(root,'state.json')==prepared.initial,'scope owner fenced')
        key,size=store._produce_artifact(artifacts,(prepared.artifact,))
        _require((key,size)==(hashlib.sha256(prepared.artifact).hexdigest(),len(prepared.artifact)),'scope artifact mismatch')
        store._write(root,'state.json',prepared.state)
    prepared.used=True;prepared.current=prepared.state;store._scope_progress=(True,prepared.state)
    admission.acknowledge_scope(prepared)
    return _parse(prepared.state).get('scope_pending')


NativeOperationStore.prepare_scope=_prepare_scope
NativeOperationStore.accept_scope=_accept_scope


def _prepare_scope_transition(store,expected_revision,transaction_id,*,cancel=False):
    with store.mutex,_publication_observe(store) as (root,artifacts):
        _require(root is not None,'pending scope required')
        initial=_parse(store._read_file(root,'state.json'));store._validate(initial,artifacts);store._cas(initial,expected_revision)
        pending=initial.get('scope_pending')
        _require(pending is not None and pending['id']==transaction_id,'current pending scope required')
        _require(initial['active'] is None and initial['recovery'] is None,'publication reconciliation required')
        record=initial['records'][pending['record_id']]
        if not cancel and record['identity']['execution_policy']!='native-scope-v2':
            raise Conflict('legacy-scope-v1-reopen-requires-explicit-migration')
        artifact=b'' if cancel else store._artifact(artifacts,record['artifact'],record['artifact_size'])
        state=copy.deepcopy(initial)
        if not cancel:
            state['scope_pending']['owner']=store.owner
            state['scope_pending']['authority_epoch']+=1
            state['records'][pending['record_id']]['status']='preview'
            state['current']['scope']=pending['record_id']
            state['revision']+=1;state['updated_at']=_now()
        if cancel:
            state['scope_pending']=None
            state['current']['scope']=None
            store._invalidate(state['records'][pending['record_id']])
            state['revision']+=1;state['updated_at']=_now()
            store._compact_graph(state)
        _publication_graph(store,state,artifacts,artifact)
        prepared=PreparedScope(store,initial,state,artifact,str(uuid.uuid4()));store._prepared_scope=prepared
        store._scope_snapshot=prepared.seal;store._scope_progress=(False,prepared.initial)
        return prepared


NativeOperationStore.prepare_scope_transition=_prepare_scope_transition



def _scope_authority(store):
    with store.mutex,_publication_observe(store) as (root,artifacts):
        if root is None:return None
        state=_parse(store._read_file(root,'state.json'));store._validate(state,artifacts)
        pending=state.get('scope_pending')
        if pending is None:return None
        if pending['owner']!=store.owner:raise Conflict('canonical scope lifecycle owner changed')
        return (state['instance_id'],pending['id'],pending['record_id'],pending['candidate_digest'],
                pending['owner'],pending['authority_epoch'])


NativeOperationStore.scope_authority=_scope_authority


def _scope_content_identity(info):
    # Reads may change atime; identity and content metadata must stay stable.
    return (info.st_dev,info.st_ino,info.st_mode,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def _scope_refs(self,state,directory):
    """Only the canonical pending scope owns payloads; retained history does not."""
    pending=state.get('scope_pending')
    if pending is None:return ()
    record=state['records'][pending['record_id']]
    if record['identity']['execution_policy']!='native-scope-v2':return ()
    import native_publication as publication
    ContentRef=publication.save.ContentRef
    value=_parse(self._artifact(directory,record['artifact'],record['artifact_size']))
    _require(value.get('profile')=='native-scope-review-v2','unsupported pending scope content profile')
    found={}
    def walk(item):
        if type(item) is ContentRef:
            previous=found.setdefault(item.key,item)
            _require(previous==item,'conflicting content descriptors')
        elif isinstance(item,(tuple,list)):
            for child in item:walk(child)
        elif hasattr(item,'__dataclass_fields__'):
            for name in item.__dataclass_fields__:walk(getattr(item,name))
    for field in ('blobs','excluded_unsaved','removed_before','baseline_blobs','review'):
        walk(publication._decode(value[field]))
    return tuple(found[key] for key in sorted(found))


@contextmanager
def _verified_scope_blob(self,directory,ref):
    import native_publication as publication
    ContentRef=publication.save.ContentRef
    _require(type(ref) is ContentRef,'typed content reference required')
    name=ref.key+'.blob'
    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
    identity=_scope_content_identity
    try:
        self._regular(fd);before=identity(os.fstat(fd))
        raw=hashlib.sha256();git=hashlib.new(ref.object_format,b'blob '+str(ref.size).encode()+b'\0')
        count=0;binary=False
        while True:
            chunk=os.read(fd,CHUNK_SIZE)
            if not chunk:break
            count+=len(chunk);_require(count<=ref.size,'content size exceeded')
            raw.update(chunk);git.update(chunk);binary=binary or b'\0' in chunk
        _require((raw.hexdigest(),count,git.hexdigest(),binary)==(ref.key,ref.size,ref.oid,ref.binary),'content descriptor mismatch')
        _require(identity(os.fstat(fd))==before==identity(os.stat(name,dir_fd=directory,follow_symlinks=False)),'content route changed')
        os.lseek(fd,0,os.SEEK_SET)
        yield fd
        _require(identity(os.fstat(fd))==before==identity(os.stat(name,dir_fd=directory,follow_symlinks=False)),'content route changed during consumption')
    finally:os.close(fd)


def _install_scope_blobs(self,prepared,admission):
    prepared.check();admission.qualify_scope(prepared)
    with _publication_observe(self) as (root,directory):
        for ref,producer in prepared.payloads:
            temporary='.tmp-'+str(uuid.uuid4())
            try:
                try:
                    with self._verified_scope_blob(directory,ref):pass
                except FileNotFoundError:
                    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
                    with os.fdopen(fd,'wb') as target:
                        from native_publication import save
                        ContentSink=save.ContentSink
                        sink=ContentSink(ref.size,ref.object_format,target)
                        producer(sink)
                        _require(sink.reference()==ref,'content producer changed')
                        target.flush();self.fault('scope_blob_written');os.fsync(target.fileno())
                    self.fault('scope_blob_fsynced')
                    # link is atomic and cannot overwrite a concurrent immutable name.
                    try:os.link(temporary,ref.key+'.blob',src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False)
                    except FileExistsError:
                        with self._verified_scope_blob(directory,ref):pass
                    os.unlink(temporary,dir_fd=directory)
                    self.fault('scope_blob_installed')
                os.fsync(directory);self.fault('scope_blob_durable')
                admission.acknowledge_scope_blob(prepared,ref)
            finally:
                try:os.unlink(temporary,dir_fd=directory)
                except FileNotFoundError:pass


NativeOperationStore._scope_refs=_scope_refs
NativeOperationStore._verified_scope_blob=_verified_scope_blob
NativeOperationStore.install_scope_blobs=_install_scope_blobs



def _validate_scope_content(self,state,directory):
    for ref in self._scope_refs(state,directory):
        with self._verified_scope_blob(directory,ref):pass


NativeOperationStore._validate_scope_content=_validate_scope_content

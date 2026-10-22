"""Bind native Source metadata separately from Master-owned descriptors."""
from app.services.federation import protocol as p
from app.services.resource_observation import digest


def source_digest(snapshot):
    value=snapshot.get('source_encryption_sha256',digest(snapshot['encryption']))
    if not isinstance(value,str) or not p.OBJECT_ID.fullmatch(value):
        raise ValueError('invalid native encryption digest')
    return value


def bind_source(snapshot,value):
    if not isinstance(value,str) or not p.OBJECT_ID.fullmatch(value):
        raise ValueError('invalid native encryption digest')
    if 'source_encryption_sha256' in snapshot and value!=source_digest(snapshot):
        raise ValueError('native encryption changed')
    master=digest(snapshot['encryption'])
    if value!=master:
        # Ordinary Direct/Relay uploads keep the public descriptor on Master,
        # while the native Source stores opaque ciphertext without a descriptor.
        # This representation requires an original ciphertext checksum. Unknown
        # checksums and any different nonempty native descriptor fail closed.
        checksum=snapshot['master'].get('etag','')
        checksum=checksum.strip('"') if isinstance(checksum,str) else ''
        if (snapshot['encryption'] is None or value!=digest(None) or
                not isinstance(checksum,str) or not p.OBJECT_ID.fullmatch(checksum)):
            raise ValueError('native encryption contradicts catalog')
    return {**snapshot,'source_encryption_sha256':value}

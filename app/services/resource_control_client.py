"""Bounded, identity-signed metadata replies on existing verified HTTPS control."""
from app.services.federation import protocol as p
from app.services.federation.transport import ControlFailure


async def signed_request(store, relation, transport, path, value, *, limit, error_type, prefix,return_envelope=False):
    try:
        envelope = await transport.request(relation['peer_endpoint'], path, method='POST', value=value,
            relation=relation['relationship_id'], credential=store.unseal(relation['credential']))
    except ControlFailure as exc:
        from app.services.resource_control_errors import verify
        try:code=verify(exc.envelope,key=relation['peer_key'],node_id=relation['peer_id'],master_id=store.node['node_id'],
            relationship_id=relation['relationship_id'],path=path,raw=p.canonical(value),status=exc.status)
        except (ValueError,TypeError,KeyError,RecursionError):
            raise error_type(prefix+'_peer_http_'+str(exc.status)) from None
        raise error_type(code) from None
    try:
        if len(p.canonical(envelope)) > limit:
            raise error_type(prefix + '_response_too_large')
        payload=p.verify(relation['peer_key'], envelope)
        return envelope if return_envelope else payload
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise error_type(prefix + '_signed_response_invalid') from exc

package node

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io/fs"
	"net/http"
	"os"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/backup"
	"github.com/wongyiuming/FrontierCloud/internal/filelease"
	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
)

type Service struct {
	repo          store.NodeRepository
	identity      *Identity
	transport     ControlClient
	pool          store.PoolRepository
	volume        BusinessVolume
	recordings    *os.Root
	backupBuilder *backup.Builder
	backups       store.BackupRepository
	wakeup        chan struct{}
}

var ErrAuthentication = errors.New("invalid relationship authentication")

func (s *Service) RequireFollower(ctx context.Context, relation store.Relationship) error {
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return err
	}
	if row.Role != "Follower" || relation.Direction != "upstream" || relation.State != "active" || relation.Protocol != protocol.Version {
		return store.ErrNodeState
	}
	return nil
}

func (s *Service) FollowerOrigin(ctx context.Context, origin string) (store.Relationship, error) {
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return store.Relationship{}, err
	}
	if row.Role != "Follower" || origin == "" {
		return store.Relationship{}, ErrCapability
	}
	relations, err := s.repo.Relationships(ctx, false)
	if err != nil {
		return store.Relationship{}, err
	}
	for _, relation := range relations {
		if relation.State == "active" && relation.Direction == "upstream" && relation.Protocol == protocol.Version && relation.Endpoint == origin {
			return relation, nil
		}
	}
	return store.Relationship{}, ErrCapability
}

func (s *Service) Confirm(ctx context.Context, relation store.Relationship) error {
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return err
	}
	if row.Role != "Follower" || relation.Direction != "upstream" {
		return store.ErrNodeState
	}
	return s.repo.ActivateRelationship(ctx, relation.ID, store.NodeAudit{Actor: relation.PeerID})
}

func (s *Service) ReceiveRevocation(ctx context.Context, relation store.Relationship) error {
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return err
	}
	apply := func() error {
		return s.repo.RevokeRelationship(ctx, relation.ID, true, store.NodeAudit{Actor: relation.PeerID})
	}
	if row.Role == "Follower" {
		if relation.Direction != "upstream" || s.volume == nil {
			return store.ErrNodeState
		}
		return s.volume.WithPromotion(ctx, "Follower", func(store.NodePromotion) error { return s.emptyRecordings(ctx, apply) })
	}
	if row.Role != "Master" || relation.Direction != "downstream" {
		return store.ErrNodeState
	}
	return apply()
}

type BusinessVolume interface {
	WithPromotion(context.Context, string, func(store.NodePromotion) error) error
	PhysicalCapacity(context.Context) (int64, int64, error)
	ControlLease(context.Context) (func(), error)
}

// ConfigureVolumes is a startup-only operation. The caller owns the recording
// root lifetime, which must outlive this service and its control loop.
func (s *Service) ConfigureVolumes(pool store.PoolRepository, volume BusinessVolume, recordings *os.Root) {
	s.pool, s.volume, s.recordings = pool, volume, recordings
}

func NewService(repo store.NodeRepository, identity *Identity, transport ControlClient) *Service {
	return &Service{repo: repo, identity: identity, transport: transport, wakeup: make(chan struct{}, 1)}
}

func (s *Service) emptyRecordings(ctx context.Context, apply func() error) error {
	if s.recordings == nil {
		return errors.New("recording volume is not configured")
	}
	if info, err := s.recordings.Lstat(".recordings-mutation.lock"); err == nil && !info.Mode().IsRegular() {
		return errors.New("unsafe recording volume lock")
	} else if err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	file, err := s.recordings.OpenFile(".recordings-mutation.lock", os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	release, err := filelease.Acquire(ctx, file, true)
	if err != nil {
		return err
	}
	defer release()
	err = fs.WalkDir(s.recordings.FS(), ".", func(name string, entry fs.DirEntry, walkErr error) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if walkErr != nil {
			return walkErr
		}
		if name == ".recordings-mutation.lock" {
			return nil
		}
		if entry.Type()&os.ModeSymlink != 0 || !entry.IsDir() {
			return errors.New("Follower still has recording files")
		}
		return nil
	})
	if err != nil {
		return err
	}
	return apply()
}
func (s *Service) Promote(ctx context.Context, role, origin string, allocation int64, a store.NodeAudit) (store.NodeIdentity, error) {
	if s.volume == nil || s.pool == nil {
		return store.NodeIdentity{}, errors.New("business volume is not configured")
	}
	if role != "Master" && role != "Follower" {
		return store.NodeIdentity{}, errors.New("role must be Master or Follower")
	}
	if role == "Master" && (allocation < store.GiB || allocation > store.MaxStorageAllocation) {
		return store.NodeIdentity{}, errors.New("Master requires a Local Storage Allocation of at least 1 GiB")
	}
	endpoint, err := Endpoint(origin)
	if err != nil {
		return store.NodeIdentity{}, err
	}
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return store.NodeIdentity{}, err
	}
	if row.Role != "Standalone" {
		return store.NodeIdentity{}, errors.New("node role is fixed until explicit reinitialization")
	}
	public, err := s.public(row)
	if err != nil {
		return store.NodeIdentity{}, err
	}
	if _, err = s.transport.Identity(ctx, endpoint, row.ID, public, "Standalone"); err != nil {
		return store.NodeIdentity{}, err
	}
	var result store.NodeIdentity
	err = s.volume.WithPromotion(ctx, role, func(p store.NodePromotion) error {
		p.Endpoint = endpoint
		p.Allocation = allocation
		apply := func() error { var err error; result, err = s.repo.PromoteIdentity(ctx, p, a); return err }
		if role == "Follower" {
			return s.emptyRecordings(ctx, apply)
		}
		return apply()
	})
	return result, err
}

func resourceWire(c store.ResourceConfiguration) map[string]any {
	return map[string]any{"storage": map[string]any{"enabled": c.Storage.Enabled, "allocated_bytes": c.Storage.Allocation}, "compute": map[string]any{"enabled": false, "worker_slots": 0}, "backup": map[string]any{"enabled": c.Backup.Enabled}}
}
func resourceValue(raw any) (store.ResourceConfiguration, error) {
	var c store.ResourceConfiguration
	v, ok := raw.(map[string]any)
	if !ok {
		return c, errors.New("invalid resource configuration")
	}
	storage, ok1 := v["storage"].(map[string]any)
	compute, ok2 := v["compute"].(map[string]any)
	backup, ok3 := v["backup"].(map[string]any)
	enabled, ok4 := storage["enabled"].(bool)
	allocated, ok5 := intField(storage, "allocated_bytes")
	_, ok6 := compute["enabled"].(bool)
	slots, ok7 := intField(compute, "worker_slots")
	backupEnabled, ok8 := backup["enabled"].(bool)
	if !ok1 || !ok2 || !ok3 || !ok4 || !ok5 || !ok6 || !ok7 || !ok8 || allocated < 0 || allocated > store.MaxStorageAllocation || slots < 0 || slots > 256 {
		return c, errors.New("invalid resource configuration")
	}
	c.Storage.Enabled = enabled
	c.Storage.Allocation = allocated
	c.Backup.Enabled = backupEnabled
	return c, nil
}

// ReceiveHeartbeat never changes reachability. Only an outbound, authenticated
// probe can mark the peer online; inbound probes apply desired configuration.
func (s *Service) ReceiveHeartbeat(ctx context.Context, relation store.Relationship, value map[string]any) (map[string]any, error) {
	if _, err := protocol.ReadCapabilities(value); err != nil {
		return nil, err
	}
	if s.pool == nil || s.volume == nil {
		return nil, errors.New("business volume is not configured")
	}
	if mode, present := value["mode"]; present && mode != nil {
		mode, ok := mode.(string)
		if !ok || relation.Direction != "upstream" || (mode != "Relay" && mode != "Direct") {
			return nil, errors.New("invalid relationship mode")
		}
		if mode != relation.Mode {
			if err := s.repo.SetRelationshipMode(ctx, relation.ID, mode, true, store.NodeAudit{Actor: relation.PeerID}); err != nil {
				return nil, err
			}
		}
	}
	if raw, present := value["resources"]; present && raw != nil {
		if relation.Direction != "upstream" {
			return nil, errors.New("invalid resource configuration direction")
		}
		config, err := resourceValue(raw)
		if err != nil {
			return nil, err
		}
		_, free, err := s.volume.PhysicalCapacity(ctx)
		if err != nil {
			return nil, err
		}
		if err = s.pool.AcceptFollowerConfiguration(ctx, relation.ID, config, free); err != nil {
			return nil, err
		}
	}
	summary := map[string]any{"app_version": AppVersion, "protocol": protocol.Version, "capabilities": protocol.BaselineCapabilities()}
	if relation.Direction == "upstream" {
		total, free, err := s.volume.PhysicalCapacity(ctx)
		if err != nil {
			return nil, err
		}
		local, err := s.pool.FollowerSummary(ctx, free, total)
		if err != nil {
			return nil, err
		}
		for k, v := range local {
			summary[k] = v
		}
	}
	return summary, nil
}
func (s *Service) public(row store.NodeIdentity) (string, error) {
	private, err := s.identity.vault.Unseal(row.PrivateKey)
	if err != nil {
		return "", errors.New("node vault identity mismatch")
	}
	return protocol.PublicKey(private)
}
func (s *Service) SignedIdentity(ctx context.Context, challenge string) (Envelope, error) {
	if !nodeIdentifier.MatchString(challenge) {
		return Envelope{}, errors.New("invalid identity challenge")
	}
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return Envelope{}, err
	}
	public, err := s.public(row)
	if err != nil {
		return Envelope{}, err
	}
	return s.identity.signed(row, map[string]any{"node_id": row.ID, "role": row.Role, "endpoint": row.Endpoint, "public_key": public, "challenge": challenge, "protocol": protocol.Version, "app_version": AppVersion, "capabilities": protocol.BaselineCapabilities()})
}
func newCredential() (string, error) {
	value := make([]byte, 48)
	_, err := rand.Read(value)
	return protocol.Encode(value), err
}
func tokenDigest(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}
func (s *Service) CreatePair(ctx context.Context, a store.NodeAudit) (Envelope, error) {
	nonce, err := randomNodeID()
	if err != nil {
		return Envelope{}, err
	}
	token, err := newCredential()
	if err != nil {
		return Envelope{}, err
	}
	now := time.Now().Unix()
	p := store.PairPackage{Nonce: nonce, TokenHash: tokenDigest(token), ExpiresAt: now + 300}
	row, err := s.repo.IssuePair(ctx, p, now, a)
	if err != nil {
		return Envelope{}, err
	}
	public, err := s.public(row)
	if err != nil {
		return Envelope{}, err
	}
	return s.identity.signed(row, map[string]any{"node_id": row.ID, "endpoint": row.Endpoint, "public_key": public, "token": token, "nonce": nonce, "expires_at": p.ExpiresAt, "protocol": protocol.Version})
}
func checkedPackage(signed Envelope, now int64) (Peer, store.PairPackage, error) {
	v := signed.Payload
	public := textField(v, "public_key")
	expires, valid := intField(v, "expires_at")
	version, versionOK := intField(v, "protocol")
	token := textField(v, "token")
	credential, err := protocol.Decode(token)
	if protocol.Verify(public, v, signed.Signature) != nil || !valid || !versionOK || version != protocol.Version || expires <= now || expires > now+300 || !nodeIdentifier.MatchString(textField(v, "nonce")) || !nodeIdentifier.MatchString(textField(v, "node_id")) || err != nil || len(credential) != 48 {
		return Peer{}, store.PairPackage{}, errors.New("invalid or expired pairing package")
	}
	origin, err := Endpoint(textField(v, "endpoint"))
	if err != nil {
		return Peer{}, store.PairPackage{}, err
	}
	return Peer{ID: textField(v, "node_id"), Endpoint: origin, PublicKey: public, Role: "Follower"}, store.PairPackage{Nonce: textField(v, "nonce"), TokenHash: tokenDigest(token), ExpiresAt: expires}, nil
}
func (s *Service) relation(identifier string, peer Peer, credential, direction string) (store.Relationship, error) {
	sealed, err := s.identity.vault.Seal(credential)
	if err != nil {
		return store.Relationship{}, err
	}
	return store.Relationship{ID: identifier, PeerID: peer.ID, Endpoint: peer.Endpoint, PublicKey: peer.PublicKey, Credential: sealed, Direction: direction, Mode: "Relay", State: "pending", Status: "offline", PeerVersion: peer.AppVersion, Protocol: protocol.Version, Summary: map[string]any{}, CreatedAt: time.Now().Unix()}, nil
}
func (s *Service) ImportPair(ctx context.Context, signed Envelope, a store.NodeAudit) (string, error) {
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return "", err
	}
	if row.Role != "Master" {
		return "", errors.New("only Master can import a pairing package")
	}
	hint, _, err := checkedPackage(signed, time.Now().Unix())
	if err != nil {
		return "", err
	}
	peer, err := s.transport.Identity(ctx, hint.Endpoint, hint.ID, hint.PublicKey, "Follower")
	if err != nil {
		return "", err
	}
	// Unacknowledged tombstones retain encrypted credentials for safe peer notice.
	old, err := s.repo.Relationships(ctx, true)
	if err != nil {
		return "", err
	}
	for _, v := range old {
		if v.PeerID == peer.ID && v.State == "revoked" {
			if err = s.NotifyRevocation(ctx, v); err != nil {
				return "", err
			}
		}
	}
	identifier, err := randomNodeID()
	if err != nil {
		return "", err
	}
	credential, err := newCredential()
	if err != nil {
		return "", err
	}
	relation, err := s.relation(identifier, peer, credential, "downstream")
	if err != nil {
		return "", err
	}
	if err = s.repo.PrepareRelationship(ctx, relation, a); err != nil {
		return "", err
	}
	challenge, err := randomNodeID()
	if err != nil {
		return "", err
	}
	master, err := s.SignedIdentity(ctx, challenge)
	if err != nil {
		return "", err
	}
	if _, err = s.transport.Request(ctx, peer.Endpoint, "/internal/v1/pair", "POST", map[string]any{"package": signed.wire(), "relationship_id": identifier, "credential": credential, "master": master.wire()}, "", ""); err != nil {
		return "", err
	}
	if _, err = s.Call(ctx, relation, "/internal/v1/confirm", map[string]any{}); err != nil {
		return "", err
	}
	if err = s.repo.ActivateRelationship(ctx, identifier, a); err != nil {
		return "", err
	}
	return identifier, nil
}
func (s *Service) ConsumePair(ctx context.Context, signed, master Envelope, identifier, credential string) error {
	now := time.Now().Unix()
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return err
	}
	if row.Role != "Follower" {
		return errors.New("only Follower accepts pairing")
	}
	packagePeer, p, err := checkedPackage(signed, now)
	if err != nil {
		return err
	}
	public, err := s.public(row)
	if err != nil {
		return err
	}
	origin, err := Endpoint(row.Endpoint)
	if err != nil {
		return err
	}
	key, keyErr := protocol.Decode(credential)
	if err != nil || keyErr != nil || len(key) != 48 || !nodeIdentifier.MatchString(identifier) || packagePeer.ID != row.ID || packagePeer.PublicKey != public || packagePeer.Endpoint != origin {
		return errors.New("pairing package identity mismatch")
	}
	if err = s.repo.PairAvailable(ctx, p, now); err != nil {
		return err
	}
	v := master.Payload
	masterKey := textField(v, "public_key")
	version, valid := intField(v, "protocol")
	if !valid || version != protocol.Version || !nodeIdentifier.MatchString(textField(v, "node_id")) || textField(v, "role") != "Master" || !nodeIdentifier.MatchString(textField(v, "challenge")) || protocol.Verify(masterKey, v, master.Signature) != nil {
		return errors.New("invalid Master identity")
	}
	peer, err := s.transport.Identity(ctx, textField(v, "endpoint"), textField(v, "node_id"), masterKey, "Master")
	if err != nil {
		return err
	}
	relation, err := s.relation(identifier, peer, credential, "upstream")
	if err != nil {
		return err
	}
	return s.repo.ConsumePair(ctx, p, relation, time.Now().Unix(), store.NodeAudit{Actor: peer.ID})
}
func (s *Service) Call(ctx context.Context, relation store.Relationship, route string, value any) (map[string]any, error) {
	credential, err := s.identity.vault.Unseal(relation.Credential)
	if err != nil {
		return nil, errors.New("relationship credential unavailable")
	}
	method := "GET"
	if value != nil {
		method = "POST"
	}
	return s.transport.Request(ctx, relation.Endpoint, route, method, value, relation.ID, credential)
}
func (s *Service) Authenticate(ctx context.Context, headers http.Header, method, path string, body []byte, pending, revoked bool) (store.Relationship, error) {
	values := map[string]string{}
	for _, name := range []string{"X-Node-Relationship", "X-Node-Time", "X-Node-Nonce", "X-Node-Signature"} {
		if len(headers.Values(name)) != 1 {
			return store.Relationship{}, ErrAuthentication
		}
		values[name] = headers.Get(name)
	}
	relation, err := s.repo.Relationship(ctx, values["X-Node-Relationship"])
	if err != nil {
		if errors.Is(err, store.ErrNodeState) {
			return store.Relationship{}, ErrAuthentication
		}
		return store.Relationship{}, err
	}
	credential, err := s.identity.vault.Unseal(relation.Credential)
	if err != nil {
		return store.Relationship{}, ErrAuthentication
	}
	now := time.Now().Unix()
	nonce, err := protocol.VerifyAuth(credential, values, method, path, body, now)
	if err != nil {
		return store.Relationship{}, ErrAuthentication
	}
	result, err := s.repo.ReserveNodeNonce(ctx, relation.ID, nonce, relation.Credential, now, pending, revoked)
	if errors.Is(err, store.ErrNodeState) {
		err = ErrAuthentication
	}
	return result, err
}
func (s *Service) NotifyRevocation(ctx context.Context, relation store.Relationship) error {
	ack, _ := relation.Summary["revocation_acknowledged"].(bool)
	if ack {
		return nil
	}
	if _, err := s.Call(ctx, relation, "/internal/v1/revoke", map[string]any{}); err != nil {
		return err
	}
	return s.repo.AcknowledgeRevocation(ctx, relation.ID, store.NodeAudit{Actor: "peer"})
}

func (s *Service) Status(ctx context.Context) (map[string]any, error) {
	row, err := s.repo.ReadIdentity(ctx)
	if err != nil {
		return nil, err
	}
	relations, err := s.repo.Relationships(ctx, false)
	if err != nil {
		return nil, err
	}
	return map[string]any{"node_id": row.ID, "role": row.Role, "endpoint": row.Endpoint, "protocol": protocol.Version, "app_version": AppVersion, "relationships": relations}, nil
}

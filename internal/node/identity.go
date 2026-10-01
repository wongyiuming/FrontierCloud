package node

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"

	"github.com/wongyiuming/FrontierCloud/internal/protocol"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"github.com/wongyiuming/FrontierCloud/internal/vault"
)

type Identity struct {
	store.NodeIdentity
	private ed25519.PrivateKey
	vault   *vault.Vault
}

func randomNodeID() (string, error) {
	value := make([]byte, 16)
	_, err := rand.Read(value)
	return hex.EncodeToString(value), err
}

func (n *Identity) signed(row store.NodeIdentity, payload map[string]any) (Envelope, error) {
	private, err := n.vault.Unseal(row.PrivateKey)
	if err != nil {
		return Envelope{}, fmt.Errorf("decrypt persistent node identity: %w", err)
	}
	signature, err := protocol.Sign(private, payload)
	return Envelope{payload, signature}, err
}

func Initialize(ctx context.Context, repo store.NodeRepository, secrets string) (*Identity, error) {
	v, err := vault.Open(secrets)
	if err != nil {
		return nil, err
	}
	_, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	encrypted, err := v.Seal(protocol.Encode(private.Seed()))
	if err != nil {
		return nil, err
	}
	id := make([]byte, 16)
	if _, err = rand.Read(id); err != nil {
		return nil, err
	}
	row, err := repo.InitializeIdentity(ctx, store.NodeIdentity{ID: hex.EncodeToString(id), Role: "Standalone", PrivateKey: encrypted, CreatedAt: time.Now().Unix()})
	if err != nil {
		return nil, err
	}
	encoded, err := v.Unseal(row.PrivateKey)
	if err != nil {
		return nil, fmt.Errorf("decrypt persistent node identity: %w", err)
	}
	seed, err := protocol.Decode(encoded)
	if err != nil || len(seed) != ed25519.SeedSize {
		return nil, fmt.Errorf("invalid node private key")
	}
	return &Identity{row, ed25519.NewKeyFromSeed(seed), v}, nil
}

func (n *Identity) KaraokeHandle(id string, global bool) (string, error) {
	kind := "standalone"
	if global {
		kind = "global"
	}
	encoded, err := json.Marshal(map[string]any{"v": 1, "kind": kind, "id": id})
	if err != nil {
		return "", err
	}
	return n.vault.Seal(string(encoded))
}
func (n *Identity) ResolveKaraoke(token string) (string, string, error) {
	if len(token) < 80 || len(token) > 512 {
		return "", "", ErrCapability
	}
	raw, e := n.vault.Unseal(token)
	if e != nil {
		return "", "", ErrCapability
	}
	var v struct {
		Version int    `json:"v"`
		Kind    string `json:"kind"`
		ID      string `json:"id"`
	}
	if json.Unmarshal([]byte(raw), &v) != nil || v.Version != 1 || !resourceIdentifier.MatchString(v.ID) {
		return "", "", ErrCapability
	}
	if v.Kind == "local" {
		v.Kind = "standalone"
	}
	if v.Kind == "remote" {
		v.Kind = "global"
	}
	if v.Kind != "standalone" && v.Kind != "global" {
		return "", "", ErrCapability
	}
	return v.Kind, v.ID, nil
}

func ResourceID(owner, id string) string {
	sum := sha256.Sum256([]byte(owner + ":" + id))
	return hex.EncodeToString(sum[:])
}

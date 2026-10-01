// Package vault implements the existing Fernet envelope using standard Go
// cryptography. Persisted node keys and opaque browser handles survive a
// runtime switch; no plaintext credential is written to the database.
package vault

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"time"
)

var ErrInvalidToken = errors.New("invalid vault token")

type Vault struct{ key [32]byte }

func New(encoded string) (*Vault, error) {
	raw, err := base64.URLEncoding.DecodeString(strings.TrimSpace(encoded))
	if err != nil || len(raw) != 32 {
		return nil, errors.New("invalid persistent vault key")
	}
	v := &Vault{}
	copy(v.key[:], raw)
	return v, nil
}

// Open creates a durable 0600 key using an exclusive hard-link publish, matching
// the reference initializer. Concurrent startups must read the same winner.
func Open(directory string) (*Vault, error) {
	if err := os.MkdirAll(directory, 0700); err != nil {
		return nil, err
	}
	path := filepath.Join(directory, "node-vault.key")
	info, err := os.Lstat(path)
	if err == nil && (info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular()) {
		return nil, errors.New("vault key must be a regular file")
	}
	if errors.Is(err, os.ErrNotExist) {
		key := make([]byte, 32)
		if _, err = rand.Read(key); err != nil {
			return nil, err
		}
		tmp, err := os.CreateTemp(directory, ".node-vault-")
		if err != nil {
			return nil, err
		}
		defer os.Remove(tmp.Name())
		if _, err = tmp.WriteString(base64.URLEncoding.EncodeToString(key)); err == nil {
			err = tmp.Sync()
		}
		closeErr := tmp.Close()
		if err != nil {
			return nil, err
		}
		if closeErr != nil {
			return nil, closeErr
		}
		if err = os.Link(tmp.Name(), path); err != nil && !errors.Is(err, os.ErrExist) {
			return nil, err
		}
	} else if err != nil {
		return nil, err
	}
	key, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return New(string(key))
}

func (v *Vault) Seal(plaintext string) (string, error) {
	block, err := aes.NewCipher(v.key[16:])
	if err != nil {
		return "", err
	}
	raw := []byte(plaintext)
	padding := aes.BlockSize - len(raw)%aes.BlockSize
	for range padding {
		raw = append(raw, byte(padding))
	}
	token := make([]byte, 25+len(raw))
	token[0] = 0x80
	binary.BigEndian.PutUint64(token[1:9], uint64(time.Now().Unix()))
	if _, err = rand.Read(token[9:25]); err != nil {
		return "", err
	}
	cipher.NewCBCEncrypter(block, token[9:25]).CryptBlocks(token[25:], raw)
	mac := hmac.New(sha256.New, v.key[:16])
	mac.Write(token)
	token = append(token, mac.Sum(nil)...)
	return base64.URLEncoding.EncodeToString(token), nil
}

func (v *Vault) Unseal(encoded string) (string, error) {
	token, err := base64.URLEncoding.DecodeString(encoded)
	if err != nil || len(token) < 73 || token[0] != 0x80 || (len(token)-57)%16 != 0 {
		return "", ErrInvalidToken
	}
	message, signature := token[:len(token)-32], token[len(token)-32:]
	mac := hmac.New(sha256.New, v.key[:16])
	mac.Write(message)
	if !hmac.Equal(signature, mac.Sum(nil)) {
		return "", ErrInvalidToken
	}
	block, _ := aes.NewCipher(v.key[16:])
	raw := make([]byte, len(message)-25)
	cipher.NewCBCDecrypter(block, message[9:25]).CryptBlocks(raw, message[25:])
	padding := int(raw[len(raw)-1])
	if padding < 1 || padding > aes.BlockSize || padding > len(raw) {
		return "", ErrInvalidToken
	}
	for _, value := range raw[len(raw)-padding:] {
		if int(value) != padding {
			return "", ErrInvalidToken
		}
	}
	return string(raw[:len(raw)-padding]), nil
}

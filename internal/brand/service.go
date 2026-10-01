// Package brand implements durable, content-addressed brand assets.
package brand

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"sync"
	"time"
)

const MaxBytes = 8 * 1024 * 1024

var ErrKind = errors.New("未知 Logo 类型")
var ErrImage = errors.New("仅支持真实 PNG 或 WebP 图片")
var ErrSize = errors.New("Logo 文件不能超过 8 MiB")

type metadata struct{ label, filename, download string }

var brands = map[string]metadata{
	"entertainment": {"前沿娱乐", "frontier-entertainment.webp", "frontier-entertainment-logo"},
	"media":         {"前沿媒体", "frontier-media.webp", "frontier-media-logo"},
	"music":         {"前沿音乐", "frontier-music.webp", "frontier-music-logo"},
}

type Service struct {
	custom   *os.Root
	defaults *os.Root
	mu       sync.RWMutex
}
type Logo struct {
	Kind        string    `json:"kind"`
	Label       string    `json:"label"`
	Source      string    `json:"source"`
	Custom      bool      `json:"custom"`
	Format      string    `json:"format"`
	SizeBytes   int64     `json:"size_bytes"`
	Version     string    `json:"version"`
	URL         string    `json:"url"`
	Payload     []byte    `json:"-"`
	ContentType string    `json:"-"`
	Filename    string    `json:"-"`
	Modified    time.Time `json:"-"`
}

func New(dataRoot, staticRoot string) (*Service, error) {
	data, err := os.OpenRoot(dataRoot)
	if err != nil {
		return nil, err
	}
	defer data.Close()
	if info, err := data.Lstat("brand"); err == nil && (!info.IsDir() || info.Mode()&os.ModeSymlink != 0) {
		return nil, errors.New("brand directory must not be a symlink")
	}
	if err := data.MkdirAll("brand", 0755); err != nil {
		return nil, err
	}
	custom, err := data.OpenRoot("brand")
	if err != nil {
		return nil, err
	}
	static, err := os.OpenRoot(staticRoot)
	if err != nil {
		custom.Close()
		return nil, err
	}
	defaults, err := static.OpenRoot("brand")
	static.Close()
	if err != nil {
		custom.Close()
		return nil, err
	}
	return &Service{custom: custom, defaults: defaults}, nil
}
func (s *Service) Close() error { return errors.Join(s.custom.Close(), s.defaults.Close()) }
func logoFile(root *os.Root, name string) ([]byte, os.FileInfo, error) {
	info, err := root.Lstat(name)
	if err != nil {
		return nil, nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, nil, errors.New("logo must be a regular file")
	}
	f, err := root.Open(name)
	if err != nil {
		return nil, nil, err
	}
	defer f.Close()
	payload, err := io.ReadAll(io.LimitReader(f, MaxBytes+1))
	if err != nil {
		return nil, nil, err
	}
	if len(payload) > MaxBytes {
		return nil, nil, ErrSize
	}
	return payload, info, nil
}
func (s *Service) effective(kind string) (Logo, error) {
	meta, ok := brands[kind]
	if !ok {
		return Logo{}, ErrKind
	}
	var payload []byte
	var info os.FileInfo
	source, format := "default", "webp"
	found := false
	for _, suffix := range []string{"png", "webp"} {
		if info, err := s.custom.Lstat(kind + "." + suffix); err == nil && !info.Mode().IsRegular() {
			continue
		}
		data, stat, err := logoFile(s.custom, kind+"."+suffix)
		if err == nil {
			payload = data
			info = stat
			source = "custom"
			format = suffix
			found = true
			break
		}
		if !errors.Is(err, os.ErrNotExist) {
			return Logo{}, err
		}
	}
	if !found {
		var err error
		payload, info, err = logoFile(s.defaults, meta.filename)
		if err != nil {
			return Logo{}, err
		}
	}
	sum := sha256.Sum256(payload)
	version := hex.EncodeToString(sum[:8])
	return Logo{Kind: kind, Label: meta.label, Source: source, Custom: source == "custom", Format: format, SizeBytes: int64(len(payload)), Version: version, URL: "/api/v1/media/brand/logo/" + kind + "?v=" + version, Payload: payload, ContentType: "image/" + format, Filename: meta.download + "." + format, Modified: info.ModTime()}, nil
}
func (s *Service) Effective(kind string) (Logo, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.effective(kind)
}
func inspect(data []byte) (string, error) {
	if len(data) == 0 {
		return "", errors.New("Logo 文件为空")
	}
	if len(data) > MaxBytes {
		return "", ErrSize
	}
	if bytes.HasPrefix(data, []byte{137, 80, 78, 71, 13, 10, 26, 10}) {
		return "png", nil
	}
	if len(data) >= 12 && string(data[:4]) == "RIFF" && string(data[8:12]) == "WEBP" {
		return "webp", nil
	}
	return "", ErrImage
}
func (s *Service) Upload(kind string, data []byte) (Logo, error) {
	if _, ok := brands[kind]; !ok {
		return Logo{}, ErrKind
	}
	format, err := inspect(data)
	if err != nil {
		return Logo{}, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	id := make([]byte, 16)
	if _, err := rand.Read(id); err != nil {
		return Logo{}, err
	}
	name := "." + kind + "-" + hex.EncodeToString(id) + ".tmp"
	f, err := s.custom.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
	if err != nil {
		return Logo{}, err
	}
	defer s.custom.Remove(name)
	_, err = f.Write(data)
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return Logo{}, err
	}
	if closeErr != nil {
		return Logo{}, closeErr
	}
	if err := s.custom.Rename(name, kind+"."+format); err != nil {
		return Logo{}, err
	}
	other := "png"
	if format == "png" {
		other = "webp"
	}
	if err := s.custom.Remove(kind + "." + other); err != nil && !errors.Is(err, os.ErrNotExist) {
		return Logo{}, err
	}
	return s.effective(kind)
}
func (s *Service) Delete(kind string) (Logo, bool, error) {
	if _, ok := brands[kind]; !ok {
		return Logo{}, false, ErrKind
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	changed := false
	for _, format := range []string{"png", "webp"} {
		info, err := s.custom.Lstat(kind + "." + format)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return Logo{}, false, err
		}
		if !info.Mode().IsRegular() {
			return Logo{}, false, errors.New("拒绝删除符号链接 Logo")
		}
	}
	for _, format := range []string{"png", "webp"} {
		err := s.custom.Remove(kind + "." + format)
		if err == nil {
			changed = true
		} else if !errors.Is(err, os.ErrNotExist) {
			return Logo{}, changed, err
		}
	}
	logo, err := s.effective(kind)
	return logo, changed, err
}

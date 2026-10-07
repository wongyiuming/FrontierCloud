package media

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"

	"github.com/wongyiuming/FrontierCloud/internal/store"
)

const defaultLyric = "lyrics/default.lrc"
const defaultContent = "[00:00.00]建设中，暂无歌词\n"
const maxLyricBytes = 5 * 1024 * 1024

var timeTag = regexp.MustCompile(`\[(\d{1,3}):([0-5]\d)(?:[\.:](\d{1,3}))?\]`)
var offsetTag = regexp.MustCompile(`(?i)\[offset:([+-]?\d+)\]`)

type LyricEntry struct {
	Time float64 `json:"time"`
	Text string  `json:"text"`
}

func ParseLRC(payload []byte) ([]LyricEntry, error) {
	payload = bytes.TrimPrefix(payload, []byte{0xef, 0xbb, 0xbf})
	if len(payload) == 0 || !utf8.Valid(payload) || bytes.IndexByte(payload, 0) >= 0 {
		return nil, errors.New("歌词文件必须使用非空 UTF-8 编码")
	}
	content := string(payload)
	offset := int64(0)
	if match := offsetTag.FindStringSubmatch(content); match != nil {
		var err error
		offset, err = strconv.ParseInt(match[1], 10, 64)
		if err != nil || offset < -1_000_000_000 || offset > 1_000_000_000 {
			return nil, errors.New("invalid lyric offset")
		}
	}
	entries := []LyricEntry{}
	seen := map[string]bool{}
	// Python splitlines includes CR, VT, FF, NEL and Unicode separators.
	lines := strings.FieldsFunc(content, func(r rune) bool {
		return r == '\n' || r == '\r' || r == '\v' || r == '\f' || (r >= 0x1c && r <= 0x1e) || r == 0x85 || r == 0x2028 || r == 0x2029
	})
	for _, line := range lines {
		matches := timeTag.FindAllStringSubmatch(line, -1)
		if len(matches) == 0 {
			continue
		}
		text := strings.TrimSpace(timeTag.ReplaceAllString(line, ""))
		if text == "" {
			continue
		}
		if utf8.RuneCountInString(text) > 4000 {
			return nil, errors.New("单行歌词最多允许 4000 个字符")
		}
		for _, match := range matches {
			minutes, _ := strconv.ParseInt(match[1], 10, 64)
			seconds, _ := strconv.ParseInt(match[2], 10, 64)
			fraction := int64(0)
			if match[3] != "" {
				fraction, _ = strconv.ParseInt(match[3], 10, 64)
				for i := len(match[3]); i < 3; i++ {
					fraction *= 10
				}
			}
			ms := max(int64(0), minutes*60_000+seconds*1000+fraction+offset)
			key := strconv.FormatInt(ms, 10) + ":" + text
			if seen[key] {
				continue
			}
			seen[key] = true
			entries = append(entries, LyricEntry{float64(ms) / 1000, text})
			if len(entries) > 10_000 {
				return nil, errors.New("歌词最多允许 10000 行")
			}
		}
	}
	if len(entries) == 0 {
		return nil, errors.New("LRC 歌词没有可展示的时间轴内容")
	}
	sort.SliceStable(entries, func(i, j int) bool { return entries[i].Time < entries[j].Time })
	return entries, nil
}

func (s *Service) ensureDefaultLyric() error {
	if err := s.root.MkdirAll("lyrics", 0755); err != nil {
		return err
	}
	if info, err := s.root.Lstat(defaultLyric); err == nil {
		if info.Mode()&os.ModeSymlink != 0 {
			if err := s.root.Remove(defaultLyric); err != nil {
				return err
			}
		} else if !info.Mode().IsRegular() {
			return errors.New("default lyric path is not a regular file")
		}
	}
	if current, err := s.root.ReadFile(defaultLyric); err == nil && string(current) == defaultContent {
		return nil
	}
	random := make([]byte, 16)
	if _, err := rand.Read(random); err != nil {
		return err
	}
	name := "lyrics/.default-" + hex.EncodeToString(random) + ".tmp"
	f, err := s.root.OpenFile(name, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0644)
	if err != nil {
		return err
	}
	defer s.root.Remove(name)
	_, err = f.WriteString(defaultContent)
	if err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return s.root.Rename(name, defaultLyric)
}

func (s *Service) validLyric(name string) bool {
	parts := strings.Split(name, "/")
	if len(parts) < 2 || len(parts) > 4 || parts[0] != "lyrics" || !validExt("lyrics", name) {
		return false
	}
	info, err := s.safeInfo(name)
	return err == nil && info.Mode().IsRegular()
}

func (s *Service) lyricFor(ctx context.Context, track store.MediaObject) (string, error) {
	name, err := s.repository.LyricPath(ctx, track.ID)
	if err != nil {
		return "", err
	}
	if s.validLyric(name) {
		return name, nil
	}
	if err := s.ensureDefaultLyric(); err != nil {
		return "", err
	}
	if err := s.repository.BindLyric(ctx, track, store.MediaObject{Path: defaultLyric, Kind: "lyric"}); err != nil {
		return "", err
	}
	return defaultLyric, nil
}

func (s *Service) Lyrics(ctx context.Context, name string) ([]LyricEntry, error) {
	release, leaseErr := s.acquire(ctx, false)
	if leaseErr != nil {
		return nil, leaseErr
	}
	defer release()
	if err := s.ready(); err != nil {
		return nil, err
	}
	track, err := s.ValidateTrack(name)
	if err != nil || track.Kind != "audio" {
		return nil, ErrLyrics
	}
	ids, err := s.repository.EnsureObjects(ctx, []store.MediaObject{track})
	if err != nil {
		return nil, err
	}
	track.ID = ids[track.Path]
	lyric, err := s.lyricFor(ctx, track)
	if err != nil {
		return nil, err
	}
	f, err := s.root.Open(lyric)
	if err != nil {
		return nil, ErrLyrics
	}
	defer f.Close()
	payload, err := io.ReadAll(io.LimitReader(f, maxLyricBytes+1))
	if err != nil {
		return nil, err
	}
	if len(payload) > maxLyricBytes {
		return nil, ErrLyrics
	}
	entries, err := ParseLRC(payload)
	if err != nil {
		return nil, ErrLyrics
	}
	return entries, nil
}

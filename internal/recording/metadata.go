// Package recording owns private, bounded recording storage. Recorded bytes
// remain opaque; only the optional versioned footer is interpreted.
package recording

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"github.com/wongyiuming/FrontierCloud/internal/store"
	"io"
	"unicode/utf8"
)

const TrailerMagic = "FRONTIERCLOUD-KARAOKE-V1"

func Metadata(file io.ReaderAt, size int64) *store.RecordingMetadata {
	tailSize := int64(8 + len(TrailerMagic))
	if size < tailSize {
		return nil
	}
	tail := make([]byte, tailSize)
	if _, e := file.ReadAt(tail, size-tailSize); e != nil || string(tail[8:]) != TrailerMagic {
		return nil
	}
	length := binary.BigEndian.Uint64(tail[:8])
	if length == 0 || length > store.MaxRecordingMetadata || length > uint64(size-tailSize) {
		return nil
	}
	raw := make([]byte, int(length))
	if _, e := file.ReadAt(raw, size-tailSize-int64(length)); e != nil || !utf8.Valid(raw) {
		return nil
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value struct {
		Version json.Number `json:"version"`
		Title   string      `json:"title"`
		Lyrics  []struct {
			Time json.Number `json:"time"`
			Text string      `json:"text"`
		} `json:"lyrics"`
	}
	if decoder.Decode(&value) != nil {
		return nil
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return nil
	}
	version, e := value.Version.Int64()
	if e != nil || version != 1 || len(value.Lyrics) > 10000 {
		return nil
	}
	result := &store.RecordingMetadata{Title: string([]rune(value.Title)[:min(255, utf8.RuneCountInString(value.Title))]), Lyrics: []store.RecordingLyric{}}
	for _, line := range value.Lyrics {
		at, e := line.Time.Float64()
		if e != nil {
			return nil
		}
		result.Lyrics = append(result.Lyrics, store.RecordingLyric{Time: at, Text: line.Text})
	}
	if !store.ValidRecordingMetadata(*result) {
		return nil
	}
	return result
}

package httpapi

import (
	"encoding/json"
	"unicode/utf8"

	"github.com/gin-gonic/gin"
)

func (a *Admin) lyricCatalog(c *gin.Context) {
	track, lyric := c.DefaultQuery("track_path", "music"), c.DefaultQuery("lyric_path", "lyrics")
	tq, lq := c.Query("track_q"), c.Query("lyric_q")
	for name, value := range map[string]string{"track_path": track, "lyric_path": lyric, "track_q": tq, "lyric_q": lq} {
		maximum := 1024
		if name == "track_q" || name == "lyric_q" {
			maximum = 100
		}
		if utf8.RuneCountInString(value) > maximum {
			invalid(c, "query", name)
			return
		}
	}
	result, err := a.public.media.LyricCatalog(c.Request.Context(), track, lyric, tq, lq)
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	c.Header("Cache-Control", "private, no-store")
	c.JSON(200, result)
}
func (a *Admin) lyricRelations(c *gin.Context) {
	var body struct {
		Kind   string   `json:"origin_kind"`
		Path   string   `json:"origin_path"`
		Linked []string `json:"linked_paths"`
	}
	if !decodeAdmin(c, &body) {
		return
	}
	if body.Linked == nil || len(body.Linked) > a.settings.AdminMaxBatchFiles {
		detail(c, 400, "关联目标无效")
		return
	}
	count, err := a.public.media.ReplaceLyrics(c.Request.Context(), body.Kind, body.Path, body.Linked, a.mutationAudit(c, "lyric_relations", body.Linked))
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	c.JSON(200, gin.H{"status": "ok", "relations": count})
}
func (a *Admin) lyricAuto(c *gin.Context) {
	var body struct {
		Manual json.RawMessage `json:"manual"`
	}
	if !decodeAdmin(c, &body) {
		return
	}
	if string(body.Manual) != "true" {
		detail(c, 400, "一键歌词关联只能由管理员手动确认触发")
		return
	}
	result, err := a.public.media.AutoLyrics(c.Request.Context(), a.mutationAudit(c, "lyric_auto_relate", nil))
	if err != nil {
		mediaAdminError(c, err)
		return
	}
	c.JSON(200, gin.H{"status": "ok", "linked": result.Linked, "preserved": result.Preserved, "ambiguous": result.Ambiguous, "unmatched": result.Unmatched})
}

use frontier_karaoke_core::LyricCue;
use serde::Deserialize;
use wasm_bindgen::{JsCast, JsValue};
use wasm_bindgen_futures::JsFuture;
use web_sys::{Request, RequestInit, RequestMode, Response};

#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct KaraokeContext {
    pub id: String,
    pub title: String,
    #[serde(rename = "type")]
    pub media_type: MediaType,
    pub stream_url: String,
    pub has_lyrics: bool,
    pub lyrics_url: Option<String>,
    pub cover_url: Option<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum MediaType {
    Audio,
    Video,
}

#[derive(Deserialize)]
struct LyricsResponse {
    entries: Vec<LyricCue>,
}

async fn get_json<T: for<'de> Deserialize<'de>>(url: &str) -> Result<T, JsValue> {
    let options = RequestInit::new();
    options.set_method("GET");
    options.set_mode(RequestMode::SameOrigin);
    let request = Request::new_with_str_and_init(url, &options)?;
    request.headers().set("Cache-Control", "no-store")?;
    let window = web_sys::window().ok_or_else(|| JsValue::from_str("window unavailable"))?;
    let response: Response = JsFuture::from(window.fetch_with_request(&request))
        .await?
        .dyn_into()?;
    if !response.ok() {
        return Err(JsValue::from_str(&format!(
            "request failed with status {}",
            response.status()
        )));
    }
    let value = JsFuture::from(response.json()?).await?;
    serde_wasm_bindgen::from_value(value).map_err(|error| JsValue::from_str(&error.to_string()))
}

pub async fn context(media: &str) -> Result<KaraokeContext, JsValue> {
    let encoded = js_sys::encode_uri_component(media);
    get_json(&format!("/api/v1/karaoke/context?media={encoded}")).await
}

pub async fn lyrics(url: &str) -> Result<Vec<LyricCue>, JsValue> {
    Ok(get_json::<LyricsResponse>(url).await?.entries)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn response_contract_deserializes() {
        let value = r#"{"id":"opaque","title":"Song","type":"audio","stream_url":"/stream","has_lyrics":true,"lyrics_url":"/lyrics","cover_url":null}"#;
        let context: KaraokeContext = serde_json::from_str(value).unwrap();
        assert_eq!(context.media_type, MediaType::Audio);
    }

    #[test]
    fn openapi_snapshot_contains_every_client_field() {
        let contract = include_str!("../../../../contracts/karaoke-openapi.json");
        for field in [
            "id",
            "title",
            "type",
            "stream_url",
            "has_lyrics",
            "lyrics_url",
            "cover_url",
        ] {
            assert!(
                contract.contains(&format!("\"{field}\"")),
                "missing {field} in contract"
            );
        }
    }
}

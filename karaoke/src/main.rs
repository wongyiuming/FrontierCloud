use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{mpsc, Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const INDEX: &str = include_str!("../static/index.html");
const SCRIPT: &str = include_str!("../static/app.js");
const STYLE: &str = include_str!("../static/style.css");

fn main() -> std::io::Result<()> {
    let listener = TcpListener::bind("0.0.0.0:8090")?;
    println!("{{\"level\":\"INFO\",\"component\":\"karaoke\",\"event\":\"started\",\"port\":8090}}");
    let (sender, receiver) = mpsc::sync_channel::<TcpStream>(64);
    let receiver = Arc::new(Mutex::new(receiver));
    for _ in 0..2 {
        let receiver = Arc::clone(&receiver);
        thread::spawn(move || loop {
            let connection = receiver.lock().expect("worker queue poisoned").recv();
            match connection {
                Ok(stream) => if let Err(error) = serve(stream) {
                    eprintln!("{{\"level\":\"WARN\",\"component\":\"karaoke\",\"event\":\"request_failed\",\"error\":{:?}}}", error.to_string());
                },
                Err(_) => break,
            }
        });
    }
    for connection in listener.incoming() {
        match connection {
            Ok(stream) => match sender.try_send(stream) {
                Ok(()) => (),
                Err(mpsc::TrySendError::Full(mut stream)) => { let _ = response(&mut stream, 503, "text/plain; charset=utf-8", b"Busy", false); },
                Err(mpsc::TrySendError::Disconnected(_)) => break,
            },
            Err(error) => eprintln!("{{\"level\":\"WARN\",\"component\":\"karaoke\",\"event\":\"accept_failed\",\"error\":{:?}}}", error.to_string()),
        }
    }
    Ok(())
}

fn serve(mut stream: TcpStream) -> std::io::Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(3)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;
    let mut buffer = [0_u8; 8192];
    let count = stream.read(&mut buffer)?;
    if count == 0 || count == buffer.len() {
        return response(&mut stream, 400, "text/plain; charset=utf-8", b"Bad Request", false);
    }
    let request = String::from_utf8_lossy(&buffer[..count]);
    let line = request.lines().next().unwrap_or_default();
    let mut parts = line.split_whitespace();
    let method = parts.next().unwrap_or_default();
    let target = parts.next().unwrap_or_default();
    let path = target.split('?').next().unwrap_or_default();
    let head = method == "HEAD";
    let (status, content_type, body) = match (method, path) {
        ("GET" | "HEAD", "/karaoke/") => (200, "text/html; charset=utf-8", INDEX.as_bytes()),
        ("GET" | "HEAD", "/karaoke/app.js") => (200, "text/javascript; charset=utf-8", SCRIPT.as_bytes()),
        ("GET" | "HEAD", "/karaoke/style.css") => (200, "text/css; charset=utf-8", STYLE.as_bytes()),
        ("GET" | "HEAD", "/health") => (200, "application/json", b"{\"status\":\"healthy\"}"),
        ("GET" | "HEAD", _) => (404, "text/plain; charset=utf-8", b"Not Found"),
        _ => (405, "text/plain; charset=utf-8", b"Method Not Allowed"),
    };
    log(method, path, status);
    response(&mut stream, status, content_type, body, head)
}

fn response(stream: &mut TcpStream, status: u16, content_type: &str, body: &[u8], head: bool) -> std::io::Result<()> {
    let reason = match status { 200 => "OK", 400 => "Bad Request", 404 => "Not Found", 405 => "Method Not Allowed", 503 => "Service Unavailable", _ => "Error" };
    write!(stream, "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n", body.len())?;
    if !head { stream.write_all(body)?; }
    stream.flush()
}

fn log(method: &str, path: &str, status: u16) {
    let timestamp = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    println!("{{\"level\":\"INFO\",\"component\":\"karaoke\",\"timestamp\":{timestamp},\"method\":{:?},\"path\":{:?},\"status\":{status}}}", method, path);
}

"""Thin HTTP/websocket client for the local ComfyUI instance."""
import requests
import websocket

HTTP_TIMEOUT_SEC = 30


class ComfyClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def system_stats(self) -> dict:
        response = requests.get(f"{self.base_url}/system_stats", timeout=HTTP_TIMEOUT_SEC)
        response.raise_for_status()
        return response.json()

    def object_info(self) -> dict:
        response = requests.get(f"{self.base_url}/object_info", timeout=HTTP_TIMEOUT_SEC)
        response.raise_for_status()
        return response.json()

    def queue_prompt(self, graph: dict, client_id: str) -> dict:
        payload = {"prompt": graph, "client_id": client_id}
        response = requests.post(f"{self.base_url}/prompt", json=payload, timeout=HTTP_TIMEOUT_SEC)
        if response.status_code == 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            error = requests.HTTPError(f"ComfyUI rejected prompt: {body}", response=response)
            error.node_errors = body.get("node_errors") or {}
            error.error_detail = body.get("error")
            raise error
        response.raise_for_status()
        return response.json()

    def history(self, prompt_id: str) -> dict:
        response = requests.get(f"{self.base_url}/history/{prompt_id}", timeout=HTTP_TIMEOUT_SEC)
        response.raise_for_status()
        return response.json()

    def interrupt(self) -> None:
        requests.post(f"{self.base_url}/interrupt", timeout=HTTP_TIMEOUT_SEC)

    def delete_queue(self) -> None:
        requests.post(f"{self.base_url}/queue", json={"clear": True}, timeout=HTTP_TIMEOUT_SEC)

    def ws_connect(self, client_id: str) -> websocket.WebSocket:
        ws_url = self.base_url.replace("http://", "ws://").replace("https://", "wss://")
        ws = websocket.WebSocket()
        ws.connect(f"{ws_url}/ws?clientId={client_id}", timeout=HTTP_TIMEOUT_SEC)
        return ws

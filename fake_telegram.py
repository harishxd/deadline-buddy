import requests

fake = {"update_id": 1, "message": {"chat": {"id": 123}, "text": "/list"}}
r = requests.post("http://127.0.0.1:5000/webhook", json=fake)
print(r.text)
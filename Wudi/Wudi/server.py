import json
import mimetypes
import os
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse


PORT = 3000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")
EMPLOYEES_FILE = os.path.join(BASE_DIR, "data", "employees.json")
EMERGENCIES_FILE = os.path.join(BASE_DIR, "data", "emergencies.json")


def read_json_file(file_path):
    with open(file_path, "r", encoding="utf-8") as file:
        return json.load(file)


def read_employees():
    return read_json_file(EMPLOYEES_FILE)


def read_emergencies():
    return read_json_file(EMERGENCIES_FILE)


def save_emergencies(emergencies):
    with open(EMERGENCIES_FILE, "w", encoding="utf-8") as file:
        json.dump(emergencies, file, indent=2)


def get_emergency_guidance(emergency_type):
    normalized_type = str(emergency_type or "").strip().lower()
    guidance = {
        "fire": "Please prioritize your safety and leave the building immediately.",
        "injury": "Please move to a safe area and seek first aid immediately.",
        "equipment failure": "Please stop using the equipment and move away from the area.",
        "electrical hazard": "Please stay away from the hazard and avoid water or electrical sources.",
        "medical emergency": "Please get immediate medical help and stay with the person if it is safe to do so.",
        "need more time": "We added time. Please check your schedule for the added time.",
        "need more tim": "We added time. Please check your schedule for the added time.",
        "other": "Please move to a safe place and follow the supervisor's instructions.",
    }
    return guidance.get(
        normalized_type,
        "Please prioritize your safety and follow the supervisor's instructions immediately.",
    )


def current_time():
    return datetime.now().astimezone().strftime("%m/%d/%Y, %I:%M:%S %p")


class EmergencyRequestHandler(BaseHTTPRequestHandler):
    def send_json(self, payload, status=200):
        response = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def read_request_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def do_GET(self):
        request_path = urlparse(self.path).path

        if request_path == "/api/employees":
            safe_employees = [
                {
                    "id": employee["id"],
                    "name": employee["name"],
                    "role": employee["role"],
                    "department": employee["department"],
                }
                for employee in read_employees()
            ]
            self.send_json(safe_employees)
            return

        if request_path == "/api/emergencies":
            self.send_json(read_emergencies())
            return

        if request_path == "/":
            request_path = "/login.html"

        self.serve_static_file(request_path)

    def do_POST(self):
        if urlparse(self.path).path != "/api/login" and urlparse(self.path).path != "/api/emergencies":
            self.send_json({"success": False, "message": "Not found."}, 404)
            return

        data = self.read_request_json()
        if data is None:
            self.send_json({"success": False, "message": "Invalid JSON request."}, 400)
            return

        if urlparse(self.path).path == "/api/login":
            employee = next(
                (
                    user
                    for user in read_employees()
                    if user.get("id") == data.get("id")
                    and user.get("password") == data.get("password")
                ),
                None,
            )
            if employee is None:
                self.send_json(
                    {"success": False, "message": "Invalid employee ID or password."},
                    401,
                )
                return

            self.send_json(
                {
                    "success": True,
                    "user": {
                        "id": employee["id"],
                        "name": employee["name"],
                        "role": employee["role"],
                        "department": employee["department"],
                    },
                }
            )
            return

        employee_id = data.get("employeeId")
        employee_name = data.get("employeeName")
        emergency_type = data.get("type")
        description = data.get("description")
        if not employee_id or not employee_name or not emergency_type or not description:
            self.send_json(
                {"success": False, "message": "Please complete all emergency fields."},
                400,
            )
            return

        new_emergency = {
            "id": int(time.time() * 1000),
            "employeeId": employee_id,
            "employeeName": employee_name,
            "type": emergency_type,
            "description": description,
            "time": current_time(),
            "status": "ACTIVE",
        }
        emergencies = read_emergencies()
        emergencies.append(new_emergency)
        save_emergencies(emergencies)
        self.send_json(
            {
                "success": True,
                "message": "Emergency submitted successfully.",
                "emergency": new_emergency,
            }
        )

    def do_PUT(self):
        path_parts = [unquote(part) for part in urlparse(self.path).path.split("/")]
        if len(path_parts) != 4 or path_parts[1:3] != ["api", "emergencies"]:
            self.send_json({"success": False, "message": "Not found."}, 404)
            return

        try:
            emergency_id = int(path_parts[3])
        except ValueError:
            self.send_json({"success": False, "message": "Emergency not found."}, 404)
            return

        data = self.read_request_json() or {}
        emergencies = read_emergencies()
        emergency = next((item for item in emergencies if item.get("id") == emergency_id), None)
        if emergency is None:
            self.send_json({"success": False, "message": "Emergency not found."}, 404)
            return

        action = str(data.get("action") or "").upper()
        if action == "REJECT":
            reason = str(data.get("reason") or "").strip()
            if not reason:
                self.send_json(
                    {"success": False, "message": "A rejection reason is required."},
                    400,
                )
                return
            emergency["status"] = "REJECTED"
            emergency["rejectReason"] = reason
            emergency["rejectedAt"] = current_time()
            emergency.pop("adminMessage", None)
            message = "Emergency rejected."
        elif action == "ACKNOWLEDGE":
            emergency["status"] = "ACKNOWLEDGED"
            emergency["adminMessage"] = str(data.get("message") or "").strip() or get_emergency_guidance(
                emergency.get("type")
            )
            emergency["acknowledgedAt"] = current_time()
            emergency.pop("rejectReason", None)
            emergency.pop("rejectedAt", None)
            message = "Emergency acknowledged."
        else:
            self.send_json({"success": False, "message": "Invalid action."}, 400)
            return

        save_emergencies(emergencies)
        self.send_json({"success": True, "message": message, "emergency": emergency})

    def serve_static_file(self, request_path):
        relative_path = os.path.normpath(unquote(request_path).lstrip("/"))
        file_path = os.path.abspath(os.path.join(PUBLIC_DIR, relative_path))
        if not file_path.startswith(os.path.abspath(PUBLIC_DIR) + os.sep) or not os.path.isfile(file_path):
            self.send_error(404, "File not found")
            return

        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        with open(file_path, "rb") as file:
            content = file.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("", PORT), EmergencyRequestHandler)
    print(f"Server running at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
from flask import Flask, request, jsonify
import threading
import time
import random
import logging
from prometheus_flask_exporter import PrometheusMetrics
from prometheus_client import Gauge, Counter, Histogram
import uuid
import datetime
from datetime import datetime, timezone

app = Flask(__name__)
metrics = PrometheusMetrics(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Custom Prometheus metrics
substation_load_gauge = Gauge('substation_current_load_kw', 'Current load of the substation in kW', ['substation_id'])
substation_capacity_gauge = Gauge('substation_max_capacity_kw', 'Maximum capacity of the substation in kW',
                                  ['substation_id'])
charging_sessions_counter = Counter('charging_sessions_total', 'Total number of charging sessions',
                                    ['substation_id', 'status'])
charging_duration_histogram = Histogram('charging_session_duration_seconds', 'Duration of charging sessions',
                                        ['substation_id'])


# Substation state
class SubstationState:
    def __init__(self, substation_id, max_capacity=100):
        self.substation_id = substation_id
        self.max_capacity = max_capacity  # kW
        self.current_load = 0  # kW
        self.active_sessions = {}
        self.session_history = []
        self.lock = threading.Lock()

        # Initialize Prometheus metrics
        substation_load_gauge.labels(substation_id=self.substation_id).set(0)
        substation_capacity_gauge.labels(substation_id=self.substation_id).set(self.max_capacity)

    def can_accept_load(self, requested_power):
        with self.lock:
            return (self.current_load + requested_power) <= self.max_capacity

    def add_charging_session(self, vehicle_id, requested_power, duration):
        # with self.lock:
        if not self.can_accept_load(requested_power):
            return None
        session_id = str(uuid.uuid4())
        session = {
            'session_id': session_id,
            'vehicle_id': vehicle_id,
            'power': requested_power,
            'duration': duration,
            'start_time': datetime.now(timezone.utc),
            'status': 'active'
        }
        self.active_sessions[session_id] = session
        self.current_load += requested_power
        substation_load_gauge.labels(substation_id=self.substation_id).set(self.current_load)
        charging_sessions_counter.labels(substation_id=self.substation_id, status='started').inc()
        threading.Thread(target=self._complete_charging, args=(session_id,), daemon=True).start()
        logger.info(f"Started charging session {session_id} for vehicle {vehicle_id}")
        return session

    def _complete_charging(self, session_id):
        if session_id in self.active_sessions:
            session = self.active_sessions[session_id]
            time.sleep(session['duration'])
            # with self.lock:
            if session_id in self.active_sessions:
                session['end_time'] = datetime.now(timezone.utc)
                session['status'] = 'completed'
                self.current_load -= session['power']
                self.session_history.append(session)
                del self.active_sessions[session_id]
                substation_load_gauge.labels(substation_id=self.substation_id).set(self.current_load)
                charging_sessions_counter.labels(substation_id=self.substation_id, status='completed').inc()
                duration_seconds = (session['end_time'] - session['start_time']).total_seconds()
                charging_duration_histogram.labels(substation_id=self.substation_id).observe(duration_seconds)
                logger.info(f"Completed charging session {session_id}")

    def get_status(self):
        # with self.lock:
        return {
                'substation_id': self.substation_id,
                'current_load': self.current_load,
                'max_capacity': self.max_capacity,
                'utilization_percent': (self.current_load / self.max_capacity) * 100,
                'active_sessions': len(self.active_sessions),
                'available_capacity': self.max_capacity - self.current_load
        }


import os

SUBSTATION_ID = os.getenv('SUBSTATION_ID', f'substation-{random.randint(1000, 9999)}')
MAX_CAPACITY = int(os.getenv('MAX_CAPACITY', '100'))
substation = SubstationState(SUBSTATION_ID, MAX_CAPACITY)

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "substation_id": SUBSTATION_ID}), 200


@app.route('/status', methods=['GET'])
def get_status():
    return jsonify(substation.get_status()), 200


@app.route('/charge', methods=['POST'])
def start_charging():
    try:
        data = request.get_json()
        if not substation.can_accept_load(data['requested_power']):
            return jsonify({
                "error": "Insufficient capacity",
                "current_load": substation.current_load,
                "max_capacity": substation.max_capacity,
                "requested_power": data['requested_power']
            }), 409

        # Start charging session
        session = substation.add_charging_session(
            data['vehicle_id'],
            data['requested_power'],
            data['duration']
        )

        if session:
            return jsonify({
                "status": "charging_started",
                "session_id": session['session_id'],
                "substation_id": SUBSTATION_ID,
                "estimated_completion": (session['start_time'] +
                                         datetime.timedelta(seconds=session['duration'])).isoformat()
            }), 200
        else:
            return jsonify({"error": "Failed to start charging session"}), 500

    except Exception as e:
        logger.error(f"Error starting charging session: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500


@app.route('/sessions', methods=['GET'])
def list_sessions():
    with substation.lock:
        return jsonify({
            "active_sessions": list(substation.active_sessions.values()),
            "completed_sessions": substation.session_history[-10:]  # Last 10 completed
        }), 200


@app.route('/sessions/<session_id>', methods=['GET'])
def get_session(session_id):
    with substation.lock:
        if session_id in substation.active_sessions:
            return jsonify(substation.active_sessions[session_id]), 200
        for session in substation.session_history:
            if session['session_id'] == session_id:
                return jsonify(session), 200
        return jsonify({"error": "Session not found"}), 404


if __name__ == '__main__':
    logger.info(f"Starting substation {SUBSTATION_ID} with capacity {MAX_CAPACITY} kW")
    app.run(host='0.0.0.0', port=5003)

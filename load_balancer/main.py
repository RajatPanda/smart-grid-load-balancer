from flask import Flask, request, jsonify
import requests
import threading
import time
import logging
from prometheus_flask_exporter import PrometheusMetrics
from prometheus_client import Gauge, Counter, Histogram
import os
from urllib.parse import urlparse
from datetime import datetime, timezone

app = Flask(__name__)
metrics = PrometheusMetrics(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Custom Prometheus metrics
load_balancer_requests = Counter('load_balancer_requests_total', 'Total requests to load balancer',
                                 ['endpoint', 'status'])
substation_assignment_time = Histogram('substation_assignment_duration_seconds',
                                       'Time spent assigning requests to substations')
active_requests_gauge = Gauge('load_balancer_active_requests', 'Number of active charging requests')


class LoadBalancer:
    def __init__(self):
        self.substations = {}
        self.active_requests = {}
        self.request_history = []
        self.lock = threading.Lock()

        # Load substation URLs from environment or use defaults
        self.substation_urls = self._get_substation_urls()

        # Start monitoring thread
        self.monitoring_thread = threading.Thread(target=self._monitor_substations, daemon=True)
        self.monitoring_thread.start()

    def _get_substation_urls(self):
        
        urls = []

        # Check for environment variable with comma-separated URLs
        env_urls = os.getenv('SUBSTATION_URLS', '')
        if env_urls:
            urls = [url.strip() for url in env_urls.split(',') if url.strip()]

        # Default URLs if none provided
        if not urls:
            urls = [
                'http://substation1:5003',
                'http://substation2:5003',
                'http://substation3:5003'
            ]

        logger.info(f"Configured substation URLs: {urls}")
        return urls

    def _monitor_substations(self):
        
        while True:
            try:
                for url in self.substation_urls:
                    self._update_substation_status(url)
                time.sleep(5)  # Update every 5 seconds
            except Exception as e:
                logger.error(f"Error in monitoring thread: {e}")
                time.sleep(5)

    def _update_substation_status(self, url):
        
        try:
            response = requests.get(f"{url}/status", timeout=5)
            if response.status_code == 200:
                status = response.json()
                substation_id = status['substation_id']

                # with self.lock:
                self.substations[substation_id] = {
                        'url': url,
                        'status': status,
                        'last_updated': time.time(),
                        'healthy': True
                    }
            else:
                self._mark_substation_unhealthy(url)

        except requests.exceptions.RequestException as e:
            logger.warning(f"Failed to get status from {url}: {e}")
            self._mark_substation_unhealthy(url)

    def _mark_substation_unhealthy(self, url):
        
        # with self.lock:
        for substation_id, info in self.substations.items():
                if info['url'] == url:
                    info['healthy'] = False
                    break

    def _select_best_substation(self, requested_power):
        # with self.lock:
        eligible_substations = []
        for substation_id, info in self.substations.items():
            if not info['healthy']:
                continue
            status = info['status']
            available_capacity = status['available_capacity']
            if available_capacity >= requested_power:
                eligible_substations.append({
                    'substation_id': substation_id,
                    'url': info['url'],
                    'current_load': status['current_load'],
                    'available_capacity': available_capacity,
                    'utilization': status['utilization_percent']
                })
        if not eligible_substations:
            return None
        eligible_substations.sort(key=lambda x: x['current_load'])
        return eligible_substations[0]

    @substation_assignment_time.time()
    def assign_request(self, request_data):
        try:
            requested_power = request_data['requested_power']
            logger.info(f"(assign_request) Received charging request: {request_data['request_id']} for {requested_power} kW")
            # Select best substation
            selected = self._select_best_substation(requested_power)

            if not selected:
                load_balancer_requests.labels(endpoint='assign', status='no_capacity').inc()
                return {
                    'error': 'No substation available',
                    'reason': 'All substations at capacity or unavailable'
                }, 503
            try:
                response = requests.post(
                    f"{selected['url']}/charge",
                    json=request_data,
                    timeout=30
                )
                if response.status_code == 200:
                    result = response.json()
                    # with self.lock:
                    self.active_requests[request_data['request_id']] = {
                            'request_data': request_data,
                            'substation_id': selected['substation_id'],
                            'substation_url': selected['url'],
                            'session_id': result.get('session_id'),
                            'status': 'assigned',
                            'assigned_at': time.time()
                        }
                    active_requests_gauge.set(len(self.active_requests))
                    load_balancer_requests.labels(endpoint='assign', status='success').inc()
                    return {
                        'status': 'assigned',
                        'substation_id': selected['substation_id'],
                        'session_id': result.get('session_id'),
                        'estimated_completion': result.get('estimated_completion'),
                        'substation_load_before': selected['current_load'],
                        'substation_capacity': selected['available_capacity'] + selected['current_load']
                    }, 200
                else:
                    logger.error(f"Substation {selected['substation_id']} rejected request: {response.text}")
                    load_balancer_requests.labels(endpoint='assign', status='substation_error').inc()
                    return {'error': 'Substation rejected request'}, response.status_code
            except requests.exceptions.RequestException as e:
                logger.error(f"Failed to communicate with substation {selected['substation_id']}: {e}")
                load_balancer_requests.labels(endpoint='assign', status='communication_error').inc()
                return {'error': 'Communication error with substation'}, 503
        except Exception as e:
            logger.error(f"Error in assign_request: {e}")
            load_balancer_requests.labels(endpoint='assign', status='internal_error').inc()
            return {'error': 'Internal server error'}, 500

    def get_request_status(self, request_id):
        # with self.lock:
        if request_id not in self.active_requests:
            for req in self.request_history:
                if req['request_data']['request_id'] == request_id:
                    return {'status': 'completed', 'details': req}, 200
            return {'error': 'Request not found'}, 404
        req_info = self.active_requests[request_id]
        try:
            response = requests.get(
                f"{req_info['substation_url']}/sessions/{req_info['session_id']}",
                timeout=10
            )
            if response.status_code == 200:
                session_data = response.json()
                req_info['session_status'] = session_data
                if session_data.get('status') == 'completed':
                    self.request_history.append(req_info)
                    del self.active_requests[request_id]
                    active_requests_gauge.set(len(self.active_requests))
                    return {'status': 'completed', 'details': req_info}, 200
        except requests.exceptions.RequestException:
            pass
        return {'status': 'active', 'details': req_info}, 200

    def get_all_requests(self):
        
        # with self.lock:
        return {
            'active_requests': list(self.active_requests.values()),
            'recent_completed': self.request_history[-20:],  # Last 20 completed
            'total_active': len(self.active_requests)
        }

    def get_system_status(self):
        
        # with self.lock:
        return {
                'substations': self.substations,
                'active_requests': len(self.active_requests),
                'total_substations': len(self.substations),
                'healthy_substations': sum(1 for info in self.substations.values() if info['healthy'])
    }


# Initialize load balancer
load_balancer = LoadBalancer()


@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "service": "load_balancer"}), 200


@app.route('/api/assign-substation', methods=['POST'])
def assign_substation():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400
        result, status_code = load_balancer.assign_request(data)
        return jsonify(result), status_code
    except Exception as e:
        logger.error(f"Error in assign_substation: {e}")
        load_balancer_requests.labels(endpoint='assign', status='error').inc()
        return jsonify({"error": "Internal server error"}), 500


@app.route('/api/status/<request_id>', methods=['GET'])
def get_request_status(request_id):
    try:
        result, status_code = load_balancer.get_request_status(request_id)
        return jsonify(result), status_code
    except Exception as e:
        logger.error(f"Error getting request status: {e}")
        return jsonify({"error": "Internal server error"}), 500


@app.route('/api/requests', methods=['GET'])
def list_requests():
    try:
        return jsonify(load_balancer.get_all_requests()), 200
    except Exception as e:
        logger.error(f"Error listing requests: {e}")
        return jsonify({"error": "Internal server error"}), 500


@app.route('/api/substations', methods=['GET'])
def list_substations():
    try:
        return jsonify(load_balancer.get_system_status()), 200
    except Exception as e:
        logger.error(f"Error listing substations: {e}")
        return jsonify({"error": "Internal server error"}), 500


@app.route('/api/system-status', methods=['GET'])
def system_status():
    try:
        return jsonify(load_balancer.get_system_status()), 200
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        return jsonify({"error": "Internal server error"}), 500


if __name__ == '__main__':
    # TODO: should be 5001
    logger.info("Starting Load Balancer service")
    app.run(host='0.0.0.0', port=5002)

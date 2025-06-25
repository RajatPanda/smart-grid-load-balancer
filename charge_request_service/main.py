from flask import Flask, request, jsonify
import requests
import logging
from prometheus_flask_exporter import PrometheusMetrics

app = Flask(__name__)
metrics = PrometheusMetrics(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
LOAD_BALANCER_URL = "http://load_balancer:5002"

@app.route('/health', methods=['GET'])
def health_check():
    
    return jsonify({"status": "healthy", "service": "charge_request_service"}), 200

@app.route('/api/charge', methods=['POST'])
def request_charge():
    
    try:
        # Get request data
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        # Validate required fields
        required_fields = ['vehicle_id', 'requested_power', 'duration']
        for field in required_fields:
            if field not in data:
                return jsonify({"error": f"Missing required field: {field}"}), 400

        # Add timestamp and request ID
        import uuid
        import datetime
        data['request_id'] = str(uuid.uuid4())
        data['timestamp'] = datetime.datetime.utcnow().isoformat()

        logger.info(f"Received charging request: {data['request_id']} for vehicle {data['vehicle_id']}")

        # Forward request to load balancer
        try:
            response = requests.post(
                f"{LOAD_BALANCER_URL}/api/assign-substation",
                json=data,
                timeout=30
            )
            response.raise_for_status()

            result = response.json()
            logger.info(f"Request {data['request_id']} assigned to substation {result.get('substation_id')}")

            return jsonify({
                "status": "accepted",
                "request_id": data['request_id'],
                "message": "Charging request submitted successfully",
                "assignment": result
            }), 200

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to communicate with load balancer: {str(e)}")
            return jsonify({
                "error": "Service temporarily unavailable",
                "request_id": data['request_id']
            }), 503

    except Exception as e:
        logger.error(f"Error processing charge request: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/api/status/<request_id>', methods=['GET'])
def get_charge_status(request_id):
    
    try:
        # Forward request to load balancer
        response = requests.get(
            f"{LOAD_BALANCER_URL}/api/status/{request_id}",
            timeout=30
        )
        response.raise_for_status()

        return response.json(), response.status_code

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to get status for request {request_id}: {str(e)}")
        return jsonify({"error": "Unable to retrieve status"}), 503

@app.route('/api/requests', methods=['GET'])
def list_requests():
    
    try:
        response = requests.get(
            f"{LOAD_BALANCER_URL}/api/requests",
            timeout=30
        )
        response.raise_for_status()

        return response.json(), response.status_code

    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to list requests: {str(e)}")
        return jsonify({"error": "Unable to retrieve requests"}), 503

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001)

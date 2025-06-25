import requests
import time
import random
import threading
import json
import uuid
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import argparse
import os

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('load_test.log')
    ]
)
logger = logging.getLogger(__name__)

class LoadTester:
    def __init__(self, base_url="http://localhost:5001"):
        self.base_url = base_url
        self.session = requests.Session()
        self.results = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'response_times': [],
            'errors': [],
            'start_time': None,
            'end_time': None
        }
        self.active_requests = []
        self.lock = threading.Lock()

    def generate_vehicle_data(self):
        """Generate realistic vehicle charging data"""
        vehicle_types = [
            {'type': 'compact', 'power_range': (7, 22), 'duration_range': (1800, 3600)},  # 30min-1hr
            {'type': 'sedan', 'power_range': (11, 43), 'duration_range': (2400, 4800)},   # 40min-1.3hr
            {'type': 'suv', 'power_range': (22, 50), 'duration_range': (3000, 5400)},     # 50min-1.5hr
            {'type': 'truck', 'power_range': (50, 150), 'duration_range': (3600, 7200)},  # 1hr-2hr
        ]

        vehicle_type = random.choice(vehicle_types)
        power = random.uniform(*vehicle_type['power_range'])
        duration = random.randint(*vehicle_type['duration_range'])

        return {
            'vehicle_id': f"EV-{random.randint(1000, 9999)}",
            'vehicle_type': vehicle_type['type'],
            'requested_power': round(power, 2),
            'duration': duration,
            'owner_id': f"user-{random.randint(100, 999)}",
            'battery_level': random.randint(10, 90),
            'target_level': random.randint(80, 100)
        }

    def send_charge_request(self, vehicle_data):
        """Send a single charge request"""
        start_time = time.time()

        try:
            response = self.session.post(
                f"{self.base_url}/api/charge",
                json=vehicle_data,
                timeout=30
            )

            end_time = time.time()
            response_time = end_time - start_time

            with self.lock:
                self.results['total_requests'] += 1
                self.results['response_times'].append(response_time)

                if response.status_code == 200:
                    self.results['successful_requests'] += 1
                    result = response.json()
                    self.active_requests.append({
                        'request_id': result.get('request_id'),
                        'vehicle_id': vehicle_data['vehicle_id'],
                        'status': 'submitted',
                        'response_time': response_time,
                        'timestamp': datetime.now().isoformat()
                    })
                    logger.info(f"✓ Request successful for {vehicle_data['vehicle_id']} "
                              f"({response_time:.2f}s)")
                else:
                    self.results['failed_requests'] += 1
                    error_info = {
                        'vehicle_id': vehicle_data['vehicle_id'],
                        'status_code': response.status_code,
                        'error': response.text,
                        'response_time': response_time
                    }
                    self.results['errors'].append(error_info)
                    logger.warning(f"✗ Request failed for {vehicle_data['vehicle_id']}: "
                                 f"{response.status_code} - {response.text}")

            return response.status_code == 200

        except Exception as e:
            end_time = time.time()
            response_time = end_time - start_time

            with self.lock:
                self.results['total_requests'] += 1
                self.results['failed_requests'] += 1
                self.results['response_times'].append(response_time)

                error_info = {
                    'vehicle_id': vehicle_data['vehicle_id'],
                    'error': str(e),
                    'response_time': response_time
                }
                self.results['errors'].append(error_info)
                logger.error(f"✗ Exception for {vehicle_data['vehicle_id']}: {e}")

            return False

    def rush_hour_simulation(self, duration_minutes=10, peak_rps=10):
        """Simulate rush hour traffic with gradual increase and decrease"""
        logger.info(f"Starting rush hour simulation for {duration_minutes} minutes")
        logger.info(f"Peak request rate: {peak_rps} requests/second")

        self.results['start_time'] = datetime.now()
        total_duration = duration_minutes * 60  # Convert to seconds

        # Rush hour pattern: gradual increase, peak, gradual decrease
        def get_request_rate(elapsed_time):
            """Calculate request rate based on elapsed time (rush hour pattern)"""
            progress = elapsed_time / total_duration

            if progress < 0.3:  # First 30% - gradual increase
                return peak_rps * (progress / 0.3)
            elif progress < 0.7:  # Middle 40% - peak traffic
                return peak_rps
            else:  # Last 30% - gradual decrease
                return peak_rps * (1 - (progress - 0.7) / 0.3)

        start_time = time.time()

        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = []

            while time.time() - start_time < total_duration:
                elapsed = time.time() - start_time
                current_rate = get_request_rate(elapsed)

                # Calculate sleep time for current rate
                if current_rate > 0:
                    sleep_time = 1.0 / current_rate

                    # Generate and submit request
                    vehicle_data = self.generate_vehicle_data()
                    future = executor.submit(self.send_charge_request, vehicle_data)
                    futures.append(future)

                    # Log progress every 30 seconds
                    if int(elapsed) % 30 == 0:
                        logger.info(f"Progress: {elapsed/60:.1f}min, "
                                  f"Rate: {current_rate:.1f} req/s, "
                                  f"Submitted: {self.results['total_requests']}")

                    time.sleep(sleep_time)
                else:
                    time.sleep(1)

            # Wait for all requests to complete
            logger.info("Waiting for all requests to complete...")
            for future in as_completed(futures, timeout=60):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Request execution error: {e}")

        self.results['end_time'] = datetime.now()
        logger.info("Rush hour simulation completed")

    def sustained_load_test(self, duration_minutes=5, rps=5):
        """Run sustained load test"""
        logger.info(f"Starting sustained load test for {duration_minutes} minutes at {rps} RPS")

        self.results['start_time'] = datetime.now()
        total_duration = duration_minutes * 60
        sleep_time = 1.0 / rps

        start_time = time.time()

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = []

            while time.time() - start_time < total_duration:
                vehicle_data = self.generate_vehicle_data()
                future = executor.submit(self.send_charge_request, vehicle_data)
                futures.append(future)

                elapsed = time.time() - start_time
                if int(elapsed) % 30 == 0:
                    logger.info(f"Progress: {elapsed/60:.1f}min, "
                              f"Submitted: {self.results['total_requests']}")

                time.sleep(sleep_time)

            # Wait for completion
            for future in as_completed(futures, timeout=60):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Request execution error: {e}")

        self.results['end_time'] = datetime.now()
        logger.info("Sustained load test completed")

    def spike_test(self, spike_duration=60, spike_rps=20):
        """Simulate sudden traffic spike"""
        logger.info(f"Starting spike test: {spike_rps} RPS for {spike_duration} seconds")

        self.results['start_time'] = datetime.now()
        sleep_time = 1.0 / spike_rps

        start_time = time.time()

        with ThreadPoolExecutor(max_workers=30) as executor:
            futures = []

            while time.time() - start_time < spike_duration:
                vehicle_data = self.generate_vehicle_data()
                future = executor.submit(self.send_charge_request, vehicle_data)
                futures.append(future)
                time.sleep(sleep_time)

            # Wait for completion
            for future in as_completed(futures, timeout=60):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"Request execution error: {e}")

        self.results['end_time'] = datetime.now()
        logger.info("Spike test completed")

    def print_results(self):
        """Print test results summary"""
        if not self.results['response_times']:
            logger.error("No data to analyze")
            return

        response_times = self.results['response_times']

        print("" + "="*60)
        print("LOAD TEST RESULTS SUMMARY")
        print("="*60)
        print(f"Test Duration: {self.results['end_time'] - self.results['start_time']}")
        print(f"Total Requests: {self.results['total_requests']}")
        print(f"Successful: {self.results['successful_requests']}")
        print(f"Failed: {self.results['failed_requests']}")
        print(f"Success Rate: {(self.results['successful_requests']/self.results['total_requests']*100):.1f}%")
        print()

        print("RESPONSE TIME STATISTICS:")
        print(f"Average: {sum(response_times)/len(response_times):.3f}s")
        print(f"Min: {min(response_times):.3f}s")
        print(f"Max: {max(response_times):.3f}s")

        sorted_times = sorted(response_times)
        print(f"P50: {sorted_times[len(sorted_times)//2]:.3f}s")
        print(f"P90: {sorted_times[int(len(sorted_times)*0.9)]:.3f}s")
        print(f"P95: {sorted_times[int(len(sorted_times)*0.95)]:.3f}s")
        print(f"P99: {sorted_times[int(len(sorted_times)*0.99)]:.3f}s")
        print()

        if self.results['errors']:
            print("ERROR SUMMARY:")
            error_types = {}
            for error in self.results['errors']:
                error_key = error.get('status_code', 'Exception')
                error_types[error_key] = error_types.get(error_key, 0) + 1

            for error_type, count in error_types.items():
                print(f"  {error_type}: {count} occurrences")

        print("="*60)

    def save_results(self, filename="load_test_results.json"):
        """Save results to JSON file"""
        # Convert datetime objects to strings for JSON serialization
        results_copy = self.results.copy()
        if results_copy['start_time']:
            results_copy['start_time'] = results_copy['start_time'].isoformat()
        if results_copy['end_time']:
            results_copy['end_time'] = results_copy['end_time'].isoformat()

        with open(filename, 'w') as f:
            json.dump(results_copy, f, indent=2)

        logger.info(f"Results saved to {filename}")

def main():
    parser = argparse.ArgumentParser(description='Smart Grid Load Tester')
    parser.add_argument('--url', default='http://localhost:5001', 
                       help='Base URL for the charge request service')
    parser.add_argument('--test-type', choices=['rush-hour', 'sustained', 'spike'], 
                       default='rush-hour', help='Type of load test to run')
    parser.add_argument('--duration', type=int, default=10, 
                       help='Test duration in minutes')
    parser.add_argument('--rps', type=int, default=10, 
                       help='Requests per second (peak for rush-hour)')
    parser.add_argument('--output', default='load_test_results.json', 
                       help='Output file for results')
    args = parser.parse_args()
    try:
        response = requests.get(f"{args.url}/health", timeout=5)
        if response.status_code != 200:
            logger.error(f"Service not healthy: {response.status_code}")
            return 1
    except Exception as e:
        logger.error(f"Cannot connect to service at {args.url}: {e}")
        logger.error("Make sure the services are running with: docker-compose up")
        return 1
    logger.info(f"Starting {args.test_type} load test")
    logger.info(f"Target URL: {args.url}")
    tester = LoadTester(args.url)
    try:
        if args.test_type == 'rush-hour':
            tester.rush_hour_simulation(args.duration, args.rps)
        elif args.test_type == 'sustained':
            tester.sustained_load_test(args.duration, args.rps)
        elif args.test_type == 'spike':
            tester.spike_test(args.duration, args.rps)
        tester.print_results()
        tester.save_results(args.output)
        logger.info("Load test completed successfully")
        return 0
    except KeyboardInterrupt:
        logger.info("Load test interrupted by user")
        tester.print_results()
        return 0
    except Exception as e:
        logger.error(f"Load test failed: {e}")
        return 1

if __name__ == "__main__":
    exit(main())

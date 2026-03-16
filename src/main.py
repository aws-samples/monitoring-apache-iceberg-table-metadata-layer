import logging
import os
import time

# Configure logging before importing monitor to ensure all loggers inherit settings
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

from monitor import start_server, update_metrics

if __name__ == '__main__':
    port = int(os.getenv('METRICS_PORT', '8000'))
    poll_interval = int(os.getenv('POLL_INTERVAL', '30'))
    
    start_server(port)
    
    while True:
        logger.info("Polling Iceberg metrics...")
        try:
            update_metrics()
        except Exception as e:
            logger.error(f"Top-level error in update_metrics: {e}")
        logger.info("Finished polling.")
        time.sleep(poll_interval)

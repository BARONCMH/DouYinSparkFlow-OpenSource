import logging
import unittest

from utils.logger import setup_logger


class LoggerTests(unittest.TestCase):
    def tearDown(self):
        logger = logging.getLogger('test-logger')
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)

    def test_setup_logger_updates_existing_handler_levels(self):
        logger = setup_logger(name='test-logger', level='Info')
        self.assertEqual([handler.level for handler in logger.handlers], [logging.INFO, logging.INFO])

        logger = setup_logger(name='test-logger', level='Debug')

        self.assertEqual(logger.level, logging.DEBUG)
        self.assertEqual([handler.level for handler in logger.handlers], [logging.DEBUG, logging.DEBUG])


if __name__ == '__main__':
    unittest.main()

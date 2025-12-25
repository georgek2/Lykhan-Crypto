import os
from dotenv import load_dotenv
'''
Docstring for agent.load

Loads the transformed data to destination.

S3torage, Databases, etc.
boto3 for AWS S3
'''


# Load to AWS S3 
class S3Loader: 

    '''
    Docstring for S3Loader
    Loads data to AWS S3 using boto3.
    '''
    AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
    AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
    AWS_BUCKET_NAME = os.getenv('AWS_BUCKET_NAME')
    
    def load_to_s3(self, data, s3_path):
        pass


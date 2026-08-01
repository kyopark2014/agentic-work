#!/usr/bin/env python3
"""
Upload content to S3 and sync Knowledge Base data source
"""

import boto3
import os
import sys
import logging
from botocore.exceptions import ClientError

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

workingDir = os.path.dirname(os.path.abspath(__file__))
_strands_dir = os.path.join(workingDir, "runtime_agent", "strands")
if _strands_dir not in sys.path:
    sys.path.insert(0, _strands_dir)

import utils  # noqa: E402

_app_config_path = os.path.join(workingDir, "application", "config.json")


def _require_config():
    """Load shared config (application/config.json via utils.load_config)."""
    config = utils.load_config(_app_config_path if os.path.isfile(_app_config_path) else None)
    if not config.get("s3_bucket") or not config.get("knowledge_base_id"):
        logger.error(
            "Missing s3_bucket/knowledge_base_id in config. "
            "Run `python installer.py` first to deploy and write application/config.json."
        )
        exit(1)
    return config

def check_file_exists_in_s3(s3_client, bucket_name, key):
    """Check if file already exists in S3"""
    try:
        s3_client.head_object(Bucket=bucket_name, Key=key)
        return True
    except ClientError as e:
        if e.response['Error']['Code'] == '404':
            return False
        raise

def get_contents_type(file_name):
    if file_name.lower().endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif file_name.lower().endswith((".pdf")):
        content_type = "application/pdf"
    elif file_name.lower().endswith((".txt")):
        content_type = "text/plain"
    elif file_name.lower().endswith((".csv")):
        content_type = "text/csv"
    elif file_name.lower().endswith((".ppt", ".pptx")):
        content_type = "application/vnd.ms-powerpoint"
    elif file_name.lower().endswith((".doc", ".docx")):
        content_type = "application/msword"
    elif file_name.lower().endswith((".xls")):
        content_type = "application/vnd.ms-excel"
    elif file_name.lower().endswith((".py")):
        content_type = "text/x-python"
    elif file_name.lower().endswith((".js")):
        content_type = "application/javascript"
    elif file_name.lower().endswith((".md")):
        content_type = "text/markdown"
    elif file_name.lower().endswith((".png")):
        content_type = "image/png"
    else:
        content_type = "no info"    
    return content_type

def upload_file_to_s3(s3_client, local_file, bucket_name, s3_key):
    """Upload file to S3"""
    try:
        with open(local_file, 'rb') as f:
            file_bytes = f.read()
        
        content_type = get_contents_type(s3_key)
        logger.info(f"Uploading {local_file} to s3://{bucket_name}/{s3_key}")
        logger.info(f"Content type: {content_type}")

        user_meta = {  # user-defined metadata
            "content_type": content_type
        }
        
        put_params = {
            'Bucket': bucket_name,
            'Key': s3_key,
            'Body': file_bytes,
            'Metadata': user_meta
        }
        
        if content_type != "no info":
            put_params['ContentType'] = content_type
        
        # Set ContentDisposition to "inline" so browser displays the file instead of downloading
        # For PDF files, this allows them to be viewed directly in the browser
        if content_type == "application/pdf":
            put_params['ContentDisposition'] = 'inline'
        
        response = s3_client.put_object(**put_params)
        logger.info(f"✓ Successfully uploaded to S3. ETag: {response.get('ETag', 'N/A')}")

        return True
    
    except FileNotFoundError:
        logger.error(f"File not found: {local_file}")
        return False
    except Exception as e:
        logger.error(f"Error uploading to S3: {str(e)}")
        return False


def sync_knowledge_base(bedrock_client, knowledge_base_id):
    """Sync Knowledge Base data source"""
    try:
        response = bedrock_client.list_data_sources(knowledgeBaseId=knowledge_base_id)
        
        if not response['dataSourceSummaries']:
            logger.error("No data sources found for knowledge base")
            return False
            
        data_source_id = response['dataSourceSummaries'][0]['dataSourceId']
        
        ingestion_response = bedrock_client.start_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id
        )
        
        job_id = ingestion_response['ingestionJob']['ingestionJobId']
        logger.info(f"✓ Started ingestion job: {job_id}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to sync knowledge base: {e}")
        return False

def main():
    config = _require_config()
    region = config['region']
    s3_bucket = config['s3_bucket']
    knowledge_base_id = config['knowledge_base_id']
    
    s3_client = boto3.client('s3', region_name=region)
    bedrock_client = boto3.client('bedrock-agent', region_name=region)
    
    local_file = "contents/error_code.pdf"
    s3_key = "docs/error_code.pdf"
    
    if not os.path.exists(local_file):
        logger.error(f"File not found: {local_file}")
        return False
    
    if check_file_exists_in_s3(s3_client, s3_bucket, s3_key):
        logger.info(f"File already exists in S3, skipping upload: {s3_key}")
    else:
        if not upload_file_to_s3(s3_client, local_file, s3_bucket, s3_key):
            return False
    
    if sync_knowledge_base(bedrock_client, knowledge_base_id):
        logger.info("✓ Knowledge Base sync initiated successfully")
        return True
    else:
        return False

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)

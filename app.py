import boto3
import os
import urllib.request
import zipfile
import subprocess
import csv
import json
from datetime import datetime
import sys
import uuid 

dynamodb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

def lambda_handler(event, context):
    bucket = event["Records"][0]["s3"]["bucket"]["name"]
    key = event["Records"][0]["s3"]["object"]["key"]
    filename = os.path.basename(key)

    input_path = f"/tmp/{filename}"
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        with open(input_path, "wb") as f:
            f.write(response["Body"].read())
            f.flush()
            os.fsync(f.fileno())
        print(f"Downloaded audio file to: {input_path}")
        print(f"File size: {os.path.getsize(input_path)} bytes")
    except Exception as e:
        print("ERROR downloading file:", str(e))
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"Failed to download audio file: {str(e)}"}),
        }
    
    input_ext = os.path.splitext(input_path)[1].lower()

    if input_ext == ".wav":
        converted_path = input_path  # No need to convert
    else:
        # Convert non-wav file to wav format
        converted_path = input_path.rsplit('.', 1)[0] + '.wav'
        ffmpeg_command = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "48000",
            "-ac", "1",
            converted_path
        ]

        result = subprocess.run(ffmpeg_command, capture_output=True, text=True)

        if result.returncode != 0:
            print("FFmpeg conversion failed:", result.stderr)
            return {
                "statusCode": 500,
                "body": json.dumps({"error": "Audio conversion failed"})
            }

        print("File successfully converted to wav:", converted_path)
        input_path = converted_path

    # Download and extract model/code
    model_url = "https://birdtag.s3.us-east-1.amazonaws.com/audio/BirdNET-Analyzer-model-V2.4.zip"
    code_url = "https://birdtag.s3.us-east-1.amazonaws.com/audio/birdnet_analyzer_code.zip"

    model_zip_path = "/tmp/model.zip"
    code_zip_path = "/tmp/code.zip"
    extract_path = "/tmp/model_dir"

    model_dir = os.path.join(extract_path, "model")
    code_dir = os.path.join(extract_path, "code")

    def is_non_empty_dir(path):
        return os.path.exists(path) and os.path.isdir(path) and os.listdir(path)

    os.makedirs(extract_path, exist_ok=True)
    
    if not is_non_empty_dir(code_dir):
        print("Downloading and extracting code...")
        urllib.request.urlretrieve(code_url, code_zip_path)
        os.makedirs(code_dir, exist_ok=True)
        with zipfile.ZipFile(code_zip_path, "r") as zip_ref:
            zip_ref.extractall(code_dir)
    else:
        print("Code directory already exists. Skipping download.")

    if not is_non_empty_dir(model_dir):
        print("Downloading and extracting model...")
        urllib.request.urlretrieve(model_url, model_zip_path)
        os.makedirs(model_dir, exist_ok=True)
        with zipfile.ZipFile(model_zip_path, "r") as zip_ref:
            zip_ref.extractall(model_dir)
    else:
        print("Model directory already exists. Skipping download.")

    config_path = os.path.join(code_dir, "birdnet_analyzer", "config.py")
    with open(config_path, "r") as f:
        original_lines = f.readlines()

    keys_to_disable = ["MODEL_PATH", "PB_MODEL", "LABELS_FILE", "MDATA_MODEL_PATH", "MODEL_TYPE", "DOWNLOAD_MODEL", "SCRIPT_DIR"]
    patched_lines = []
    for line in original_lines:
        if any(line.strip().startswith(key) for key in keys_to_disable):
            patched_lines.append("# " + line)
        else:
            patched_lines.append(line)

    patched_lines.insert(0, f'SCRIPT_DIR = "{code_dir}/birdnet_analyzer"\n')          
    patched_lines.insert(0, "DOWNLOAD_MODEL = False\n")
    patched_lines.insert(0, 'MODEL_TYPE = "tflite"\n')
    patched_lines.insert(0, f'LABELS_FILE = "{model_dir}/V2.4/BirdNET_GLOBAL_6K_V2.4_Labels.txt"\n')
    patched_lines.insert(0, f'MDATA_MODEL_PATH = "{model_dir}/V2.4/BirdNET_GLOBAL_6K_V2.4_MData_Model_FP16.tflite"\n')
    patched_lines.insert(0, f'MODEL_PATH = "{model_dir}/V2.4/BirdNET_GLOBAL_6K_V2.4_Model_FP32.tflite"\n')
    patched_lines.insert(0, "PB_MODEL = None\n")
    patched_lines.insert(0, "\n")

    with open(config_path, "w") as f:
        f.writelines(patched_lines)

    output_dir = "/tmp/output"
    os.makedirs(output_dir, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "birdnet_analyzer.analyze",
        input_path,
        "--output",
        output_dir,
        "--rtype", "csv",
        "--threads", "1",
    ]

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{code_dir}:{os.environ.get('LAMBDA_TASK_ROOT', '/opt/python')}"
    env["OMP_NUM_THREADS"] = "1"
    env["TF_NUM_INTRAOP_THREADS"] = "1"
    env["TF_NUM_INTEROP_THREADS"] = "1"
    env['TMPDIR'] = '/tmp'
    env['TEMP'] = '/tmp'
    env['NUMBA_CACHE_DIR'] = '/tmp/numba_cache/'

    print("=== About to run BirdNET subprocess ===")
    print("Command:", " ".join(command))
    print("PYTHONPATH:", env["PYTHONPATH"])

    os.makedirs('/tmp/numba_cache/', exist_ok=True)

    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=600, env=env, cwd=code_dir)
        print("=== BirdNET STDOUT ===")
        print(result.stdout)
        print("=== BirdNET STDERR ===")
        print(result.stderr)
    except subprocess.TimeoutExpired:
        print("ERROR: Subprocess timeout")
        return {"statusCode": 500, "body": json.dumps({"error": "BirdNET subprocess timeout"})}

    if result.returncode != 0:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "BirdNET CLI failed",
                "stdout": result.stdout,
                "stderr": result.stderr,
            }),
        }

    filename = os.path.splitext(filename)[0]
    result_file = os.path.join(output_dir, f"{filename}.BirdNET.results.csv")
    if not os.path.exists(result_file):
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "BirdNET output file not found",
                "expected_file": result_file,
                "output_dir_contents": os.listdir(output_dir),
            })
        }

    species_counts = {}
   
    with open(result_file, "r") as csvfile:
        reader = csv.DictReader(csvfile, delimiter=",")
        for row in reader:
            label = row["Scientific name"]
            species_counts[label] = 1

    table = dynamodb.Table("TaggedMedia")

    media_id = str(uuid.uuid4())

    table.put_item(
        Item={
            "media_id": media_id,
            "original_path": key,
            "thumbnail_path": "https://birdtag.s3.us-east-1.amazonaws.com/audio/audio-icon.png",  
            "bucket": bucket,
            "tags": species_counts,
            "timestamp": datetime.utcnow().isoformat(),
            "type": "audio"
        }
    )

    return {
        "statusCode": 200,
        "body": json.dumps({"message": "Tags extracted and saved to DynamoDB"}),
    }

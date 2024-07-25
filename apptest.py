import glob
import logging
import multiprocessing
import os
import subprocess
import time
import urllib.parse
import uuid

import boto3
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def download_video(url, save_path):
    response = requests.get(url)
    print(response.status_code)
    if response.status_code == 200:
        with open(save_path, 'wb') as f:
            f.write(response.content)
        return True
    else:
        return False

def process_video(inp, out):
    out = 'output_' + out
    commands = [
        # './spatial make -i {0} -f ou -o {1} --cdist 19.24 --hfov 63.4 --hadjust 0.02 --primary right --projection rect'.format(inp, out),
        # spatial make -i {inupt_file} -f ou -o {output_file} --cdist 19.24 --hfov 63.4 --hadjust 0.02 --primary right
        './spatial make -i {0} -f ou -o {1} --cdist 19.24 --hfov 63.4 --hadjust 0.02 --primary right --projection rect'.format(inp, out)
    ]
    for command in commands:
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        print('Running command: {}'.format(command))
        stdout, stderr = process.communicate(input='y\n')
        print('Output: {}'.format(stdout))
        if process.returncode != 0:
            print('Error: {}'.format(stderr))
            return False, ''

    s3 = boto3.client('s3')
    try:
        with open(out, 'rb') as data:
            s3.upload_fileobj(data, "spcut-output", out)
    except Exception as e:
        print(f"Error uploading file to S3: {e}")
        return False, ''

    try:
        presigned_url = s3.generate_presigned_url('get_object', Params={'Bucket': "spcut-output", 'Key': out}, ExpiresIn=3600*24)
    except Exception as e:
        print(f"Error generating pre-signed URL: {e}")
        return False, None
    
    cleanup_merged(out)

    return True, presigned_url


def process_video_ffmpeg(input_file, output_file):
    logger.info(f"Processing video with FFmpeg: {input_file}")
    
    request_id = str(uuid.uuid4())
    segment_pattern = f"segment_{request_id}_%03d.ts"
    concat_file = f"concat_list_{request_id}.txt"
    
    segment_duration = "10"
    split_command = f'ffmpeg -i {input_file} -c copy -f segment -segment_time {segment_duration} -reset_timestamps 1 {segment_pattern}'
    subprocess.run(split_command, shell=True, check=True)
    
    segment_files = sorted(glob.glob(f"segment_{request_id}_*.ts"))
    pool = multiprocessing.Pool()
    processed_segments = pool.map(process_segment, [(file, request_id) for file in segment_files])
    pool.close()
    pool.join()
    
    with open(concat_file, "w") as f:
        for segment in processed_segments:
            f.write(f"file '{segment}'\n")
    
    concat_command = f'ffmpeg -f concat -safe 0 -i {concat_file} -c copy {output_file}'
    subprocess.run(concat_command, shell=True, check=True)
    
    for file in segment_files + processed_segments + [concat_file]:
        os.remove(file)
    
    logger.info("FFmpeg processing completed successfully")
    return True

def process_segment(args):
    segment_file, request_id = args
    output_segment = f"/tmp/processed_{request_id}_{os.path.basename(segment_file)}"
    command = f'ffmpeg -i "{segment_file}" -c:v libx264 -preset ultrafast -crf 23 -vf "format=yuv420p" -c:a copy -movflags +faststart "{output_segment}"'
    
    try:
        result = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
        logger.info(f"Segment processed successfully: {output_segment}")
        return output_segment
    except subprocess.CalledProcessError as e:
        logger.error(f"Error processing segment {segment_file}: {e.stderr}")
        raise

def split_video(inp):
    logger.info(f"Starting to split video: {inp}")
    command = f'./spatialmkt --input-file {inp}.MOV'
    
    logger.info(f"Executing command: {command}")
    process = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE, 
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                universal_newlines=True)
    
    stdout, sterr = process.communicate(input='\n')
    
    if process.returncode != 0:
        logger.error(f"failed with code: {process.returncode}")
        return False, {}
    
    logger.info("split done")
    
    s3_client = boto3.client('s3')
    bucket_name = 'spcut-split'
    result = {}

    logger.info("uploading to s3")
    for suffix in ['LEFT', 'RIGHT']:
        input_file = f'{inp}_{suffix}.mov'
        output_file = f'{inp}_{suffix}_processed.mov'
        
        if not os.path.exists(input_file):
            logger.error(f"not found: {input_file}")
            return False, {}
        
        if not process_video_ffmpeg(input_file, output_file):
            logger.error(f"FFmpeg processing failed for {input_file}")
            return False, {}
        
        s3_key = output_file
        
        logger.info(f"Uploading processed file to S3: {output_file}")
        s3_client.upload_file(output_file, bucket_name, s3_key)
        
        url = s3_client.generate_presigned_url('get_object',
                                                Params={'Bucket': bucket_name,
                                                        'Key': s3_key},
                                                ExpiresIn=3600)
        result[suffix.lower()] = url
        logger.info(f"gen presigned url done {suffix}")

        os.remove(output_file)

    logger.info("all good")
    return True, result

def merge_videos(left_file, right_file, output_file):
    logger.info(f"merging: {left_file} and {right_file}")

    command = f"ffmpeg -i {left_file} -i {right_file} -filter_complex '[0:v]scale=1280:720[top]; [1:v]scale=1280:720[bottom]; [top][bottom]vstack=inputs=2' -c:v libx264 -pix_fmt yuv420p -s 1280x1440 {output_file}"
    logger.info(f"executing command: {command}")

    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    stdout, stderr = process.communicate()

    if process.returncode != 0:
        logger.error(f"merge failed: {process.returncode}")
        logger.error(f"err: {stderr}")
        return False, stderr

    logger.info("merge done")

    # Call process_video instead of uploading to S3
    success, url = process_video(output_file, output_file)
    if not success:
        logger.error(f"Failed to process merged video: {output_file}")
        return False, ""

    # Clean up the merged file
    cleanup_merged(output_file)

    return True, url

def cleanup(inp):
    logger.info(f"cleaning: {inp}")
    input_file = f"{inp}.MOV"
    if os.path.exists(input_file):
        try:
            os.remove(input_file)
            logger.info(f"deleted: {input_file}")
        except Exception as e:
            logger.error(f"failed to del {input_file}: {str(e)}")
    else:
        logger.warning(f"delete: not found: {input_file}")

    for suffix in ['LEFT', 'RIGHT']:
        pattern = f"*{suffix}.mov"
        matching_files = glob.glob(pattern)
        
        if not matching_files:
            logger.warning(f"no files found with: {pattern}")
        
        for file in matching_files:
            if os.path.exists(file):
                try:
                    os.remove(file)
                    logger.info(f"deleted: {file}")
                except Exception as e:
                    logger.error(f"Failed to delete file {file}: {str(e)}")
            else:
                logger.warning(f"File not found: {file}")

    logger.info("cleanup done")
    
def cleanup_merged(file):
    logger.info(f"cleaning merged file: {file}")
    
    if os.path.exists(file):
        try:
            os.remove(file)
            logger.info(f"deleted merged file: {file}")
        except Exception as e:
            logger.error(f"failed to delete merged file {file}: {str(e)}")
    else:
        logger.warning(f"merged file not found for deletion: {file}")
    
    logger.info("cleanup post-merge")


@app.route('/process', methods=['POST'])
def processVideo():
    data = request.json
    video_url = data.get('url')
    if not video_url:
        return jsonify({'error': 'URL not provided'}), 400

    video_name = video_url.split('/')[-1]
    output_file = video_name.split('.')[0] + '_done.mov'

    success = download_video(video_url, video_name)
    if success:
        success, url = process_video(video_name, output_file)
        if not success:
            return jsonify({'error': 'Failed to process video'}), 500
        # cleanup(video_name)
        return jsonify({'output': url}), 200
    else:
        return jsonify({'error': 'Failed to download video'}), 500
    

@app.route('/split', methods=['POST'])
def splitVideo():
    data = request.json
    video_url = data.get('url')
    if not video_url:
        return jsonify({'error': 'URL not provided'}), 400

    video_name = video_url.split('/')[-1].split('.')[0]

    success = download_video(video_url, video_name + '.MOV')
    if success:
        success, response = split_video(video_name)
        if not success:
            return jsonify({'error': 'Failed to split video'}), 500
        cleanup(video_name + '.MOV')
        return jsonify(response), 200
    else:
        return jsonify({'error': 'Failed to download video'}), 500
    
    
@app.route('/merge', methods=['POST'])
def mergeVideos():
    data = request.json
    uid = data.get('uid')
    left_url = data.get('left_url')
    right_url = data.get('right_url')
    
    if not left_url or not right_url:
        return jsonify({'error': 'Both left and right video URLs are required'}), 400
    
    def get_filename_from_url(url):
        parsed_url = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed_url.path)
        return os.path.basename(path)

    left_file = 'left_' + get_filename_from_url(left_url)
    right_file = 'right_' + get_filename_from_url(right_url)
    output_file = f"{uid}_{int(time.time())}.mov"

    if download_video(left_url, left_file) and download_video(right_url, right_file):
        success, result = merge_videos(left_file, right_file, output_file)
        
        cleanup_merged(left_file)
        cleanup_merged(right_file)
        cleanup_merged(output_file) 
        
        if success:
            return jsonify({'output': result}), 200
        else:
            return jsonify({'error': f'Failed to merge videos: {result}'}), 500
    else:
        return jsonify({'error': 'Failed to download one or both videos'}), 500

@app.route('/', methods=['GET'])
def test():
    return jsonify({'message': 'Hello World!'})

if __name__ == '__main__':
    app.run(debug=True, port=3000, host='0.0.0.0')
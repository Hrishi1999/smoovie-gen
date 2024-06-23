import glob
import os
import subprocess
import time

import boto3
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

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
    commands = [
        # './spatial make -i {0} -f ou -o {1} --cdist 19.24 --hfov 63.4 --hadjust 0.02 --primary right --projection rect'.format(inp, out),
        # spatial make -i {inupt_file} -f ou -o {output_file} --cdist 19.24 --hfov 63.4 --hadjust 0.02 --primary right
        './spatial make -i {0} -f ou -o {1} --cdist 19.24 --hfov 63.4 --hadjust 0.02 --primary right --quality 0.8'.format(inp, out)
    ]
    for command in commands:
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        print('Running command: {}'.format(command))
        print('Output: {}'.format(process.stdout.read()))
        process.wait()

    if process.returncode != 0:
        print('Error: {}'.format(process.stderr.read()))
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

    return True, presigned_url

def split_video(inp):
    commands = [
        './spatialmkt --input-file {0}.MOV'.format(inp),
    ]
    for command in commands:
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process.wait()

        if process.returncode != 0:
            return False
        
    s3_client = boto3.client('s3')
    bucket_name = 'spcut-split'
    result = {}

    for suffix in ['LEFT', 'RIGHT']:
        matching_files = [f for f in os.listdir('/') if f.endswith(f"{suffix}.mov")]
        
        if not matching_files:
            return False, {}
        
        local_file = os.path.join('/', matching_files[0])
        s3_key = f"videos/{matching_files[0]}"
        
        s3_client.upload_file(local_file, bucket_name, s3_key)
        
        url = s3_client.generate_presigned_url('get_object',
                                                Params={'Bucket': bucket_name,
                                                        'Key': s3_key},
                                                ExpiresIn=3600)
        result[suffix.lower()] = url

    return True, result


def cleanup(inp):
    input_file = f"{inp}.MOV"
    if os.path.exists(input_file):
        os.remove(input_file)
    
    for suffix in ['LEFT', 'RIGHT']:
        pattern = f"/*{suffix}.mov"
        matching_files = glob.glob(pattern)
        for file in matching_files:
            if os.path.exists(file):
                os.remove(file)

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
        cleanup()
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
    output_dir = video_name + '_split'

    success = download_video(video_url, video_name + '.MOV')
    if success:
        success, response = split_video(video_name, output_dir)
        if not success:
            return jsonify({'error': 'Failed to split video'}), 500
        return jsonify({'output': output_dir}), 200
    else:
        return jsonify({'error': 'Failed to download video'}), 500
    
@app.route('/', methods=['GET'])
def test():
    return jsonify({'message': 'Hello World!'})

if __name__ == '__main__':
    app.run(debug=True, port=80, host='0.0.0.0')
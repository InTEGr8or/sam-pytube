import json
import io
from datetime import datetime
from pytube import YouTube
import boto3
import sys
import logging
import os.path
import re
import xmltodict
from functools import reduce

S3 = boto3.client('s3', region_name="us-east-1")
vidIdPattern = re.compile(r"((?<=watch\?v=).*|(?<=.be\/)(.*))")
dyndb = boto3.client('dynamodb', region_name="us-east-1")

def log_video(videoUrl, property):
    try:
        videoId = vidIdPattern.search(videoUrl).group()
        item = {
            "VideoId": {"S": videoId},
            "VideoUrl": {"S": videoUrl}
        }
        if property:
            item[property['name']] = property['value']
        dyndb.put_item(
            TableName="videos",
            Item=item
        )
    except Exception as e:
        logging.error(dir(videoUrl), e)
        return 1

def get_captions (videoUrl):
    try:
        captions = YouTube(videoUrl).captions['a.en']
        return captions.xml_captions
    except Exception as e:
        logging.error(dir(videoUrl), e)
        return 1

def xml_to_csv(xml):
    caption_obj = xmltodict.parse(xml)['timedtext']['body']['p']
    captions_csv = ""
    captions = [c for c in caption_obj if 's' in c ]
    for caption in captions:
        time = caption['@t']
        text_list = []
        # if there is only one word in the caption
        # it will be a dict instead of a list
        # so we need to convert it to a list
        if type(caption['s']) is dict:
            caption['s'] = [caption['s']]
        for si in caption['s']:
            text_list.append(si['#text'])
        text = ' '.join(text_list)
        captions_csv += (f"{time}, {text}\n")
    return captions_csv

def get_video (videoUrl):
    try:
        yt = YouTube(videoUrl).streams.filter(
        progressive=True, subtype='mp4',
    ).order_by('filesize').desc().first()
    except Exception as e:
        logging.error(dir(videoUrl), e)
        return

    buf = io.BytesIO()
    try:
        yt.stream_to_buffer(buf)
        buf.seek(0)
        videoData = buf.read()
        return videoData
    except Exception as e:
        logging.info("NO CAPTIONS\n")

def lambda_handler(event, context):
    logging.info("HANDLER EVENT:", event)

    videoUrl = event['videoUrl']
    videoId = vidIdPattern.search(videoUrl).group()
    log_video(videoUrl, None)
    xml_captions = get_captions(videoUrl)
    
    logging.info("WRIGING XML", event)


    captionFile = f"captions/{videoId}.xml"
    S3.put_object(Bucket="civilton-youtube-content", Key=captionFile, Body=xml_captions)
    log_video(event['videoUrl'], {"name": "xml_captions", "value": {"S": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}})

    csv_captions = xml_to_csv(xml_captions)
    captionFile = f"captions/{videoId}.csv"
    S3.put_object(Bucket="civilton-youtube-content", Key=captionFile, Body=csv_captions)
    log_video(event['videoUrl'], {"name": "csv_captions", "value": {"S": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}})

    videoData = get_video(videoUrl)
    videoFIle = f"videos/{videoId}.mp4"
    S3.put_object(Bucket="civilton-youtube-content", Key=videoFIle, Body=videoData)
    log_video(event['videoUrl'], {"name": "video_file", "value": {"S": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}})

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "hello world",
            # "location": ip.text.replace("\n", "")
        }),
    }

if len(sys.argv) > 1:
    lambda_handler(json.loads(sys.argv[1]), None)
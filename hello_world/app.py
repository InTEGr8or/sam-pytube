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

# Logger writes to CloudWatch
logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3 = boto3.client('s3', region_name="us-east-1")
vidIdPattern = re.compile(r"((?<=watch\?v=).*|(?<=.be\/)(.*))")
dyndb = boto3.client('dynamodb', region_name="us-east-1")

class Result:
    def __init__(self, statusCode, body=None, message=None, error=None):
        self.statusCode = statusCode
        self.body = body
        self.error = error
        self.message = message

def update_video_table(videoUrl, properties) -> int:
    try:
        videoId = vidIdPattern.search(videoUrl).group()
        item = {
            "VideoId": {"S": videoId},
            "VideoUrl": {"S": videoUrl}
        }
        if type(properties) is dict:
            properties = [properties]
        for property in properties:
            if property:
                item[property['name']] = property['value']
            dyndb.put_item(
                TableName="videos",
                Item=item
            )
        return 0
    except Exception as e:
        logger.error('update_video_table ERROR')
        logger.error(e)
        logger.error('VIDEO_URL')
        logger.error(dir(videoUrl))
        return 1

def get_captions (videoUrl: str) -> str:
    captions = ''
    description = ''
    try:
        yt = YouTube(videoUrl)
        logger.info("GET_CAPTION")
        logger.info(len(yt.captions))
        description = yt.description
        captions = yt.captions['a.en'].xml_captions if 'a.en' in yt.captions else ''
        return captions, description
    except Exception as e:
        logger.error("GET_CAPTION_error")
        logger.error(e)
        return captions, description

def xml_to_csv(xml):
    if(len(xml) < 30):
        return ""
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

def is_head_object(Bucket, Key):
    try:
        S3.head_object(Bucket=Bucket, Key=Key)
        return True
    except:
        return False

def get_video (videoUrl) -> Result:
    try:
        yt = YouTube(videoUrl).streams.filter(
        progressive=True, subtype='mp4',
    ).order_by('filesize').desc().first()
    except Exception as e:
        logger.error("GET_VIDEO_ERROR")
        logger.error(e)
        return Result(500, error="Error getting video")

    buf = io.BytesIO()
    try:
        yt.stream_to_buffer(buf)
        buf.seek(0)
        videoData = buf.read()
        return Result(200, message="Total bytes saved: " + str(len(videoData)), body=videoData) 
    except Exception as e:
        logger.info("GET_VIDEO_ERROR - Buffer read error")
        logger.error(e)
        return Result(500, error="Buffer read error")

def lambda_handler(event, context):
    logger.info("HANDLER EVENT:")
    logger.info(event)

    # Get Video URL
    # It will be in different places depending on how the lambda is invoked
    if('videoUrl' in event):
        videoUrl = event['videoUrl']
    elif('queryStringParameters' in event and event['queryStringParameters'] and 'videoUrl' in event['queryStringParameters']):
        videoUrl = event['queryStringParameters']['videoUrl']
    else:
        return {
            'statusCode': 400,
            'body': 'No Video URL',
            'event': json.dumps(event)
        }
    videoId = vidIdPattern.search(videoUrl).group()
    update_video_table(videoUrl, None)

    # Get Captions

    logger.info("GETTING XML CAPTIONS AND DESCRIPTION")
    xml_captions, description = get_captions(videoUrl)
    logger.info("WRITING XML and DESCRIPTION TO S3")

    if len(xml_captions) > 30:
        captionFile = f"captions/{videoId}.xml"
        if not is_head_object(Bucket="civilton-youtube-content", Key=captionFile):
            S3.put_object(Bucket="civilton-youtube-content", Key=captionFile, Body=xml_captions)

    update_video_table(videoUrl, [
            {
                "name": "xml_captions", 
                "value": {"S": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            },
            {
                "name": "caption_length",
                "value": {"N": str(len(xml_captions))}
            },
            {
                "name": "description",
                "value": {"S": description}
            }
        ]
    )

    if len(xml_captions) > 30:
        logger.info("## CCONVERT XML CAPTIONS TO CSV AND WRITING CSV")
        csv_captions = xml_to_csv(xml_captions)
        captionFile = f"captions/{videoId}.csv"

        if not is_head_object(Bucket="civilton-youtube-content", Key=captionFile):
            S3.put_object(Bucket="civilton-youtube-content", Key=captionFile, Body=csv_captions)
        update_video_table(videoUrl, {"name": "csv_captions", "value": {"S": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}})

    # Get Video
    logger.info("## GETTING VIDEO AND WRITING Video File")
    videoFIle = f"videos/{videoId}.mp4"
    videoDataResult = None
    if not is_head_object(Bucket="civilton-youtube-content", Key=videoFIle):
        videoDataResult = get_video(videoUrl)
        if videoDataResult and videoDataResult.statusCode == 200:
            videoData = videoDataResult.body
            S3.put_object(Bucket="civilton-youtube-content", Key=videoFIle, Body=videoData)

    update_video_table(videoUrl, [
        {
            "name": "video_file", 
            "value": {"S": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        },
        {
            "name": "video_status",
            "value": {"S": videoDataResult.statusCode if videoDataResult else "400"}
        }
    ])

    return {
        "statusCode": 200,
        "body": videoDataResult.message if videoDataResult and videoDataResult.statusCode == 200 else  videoDataResult.error if videoDataResult else "Error getting video"
   }

if len(sys.argv) > 1:
    if os.path.exists('events/event.json'):
        f = open("events/event.json", "r")
        eventString = f.read()
        f.close()
        lambda_handler(json.loads(eventString), None)
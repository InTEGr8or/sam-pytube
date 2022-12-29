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
import openai

S3 = boto3.client('s3', region_name="us-east-1")
dyndb = boto3.client('dynamodb', region_name="us-east-1")
ssm = boto3.client('ssm', region_name="us-east-1")

configs = ssm.get_parameter(Name="/civilton/youtube/config", WithDecryption=False)['Parameter']['Value'].split(",")

BUCKET_NAME = [config for config in configs if 'bucket_name' in config][0].split("=")[1]
URL_EXPIRATION = [config for config in configs if 'url_expiration' in config][0].split("=")[1]
CHAT_GPT_TOKEN = [config for config in configs if 'CHAT_GPT_TOKEN' in config][0].split("=")[1]
MAX_TOKENS = int([config for config in configs if 'max_tokens' in config][0].split("=")[1])
TEMPERATURE = float([config for config in configs if 'temperature' in config][0].split("=")[1])

# Logger writes to CloudWatch
logger = logging.getLogger()
logger.setLevel(logging.INFO)

vidIdPattern = re.compile(r"((?<=watch\?v=).*|(?<=.be\/)(.*))")

class Result:
    def __init__(self, statusCode, body=None, message=None, error=None):
        self.statusCode = statusCode
        self.body = body
        self.error = error
        self.message = message
class Property:
    def __init__(self, name, type, value):
        self.name = name
        self.type = type
        self.value = value

def send_summary_request(text: str):
    # Use the openai.Completion.stream() method to stream the text to the GPT-3 model
    completion = openai.Completion.stream(
        model="text-davinci-002",
        temperature=0.7, # controls the creativity of the summary
        max_tokens=100, # maximum number of tokens in the summary
    )

    # Send the text to the GPT-3 model in chunks of 2049 tokens or fewer
    for i in range(0, len(text), 2049):
        chunk = text[i:i+2049]
        completion.add_input(prompt=chunk)

    # Get the summary from the GPT-3 model
    summary = completion.get_response()["choices"][0]["text"]

def get_summary(text: str) -> str:
    max_total_tokens = 2049
    chunks = [text[i:i+max_total_tokens] for i in range(0, len(text), max_total_tokens)]
    max_summary_tokens = int(MAX_TOKENS / len(chunks))
    summaries = []
    for chunck in chunks:
        response = openai.Completion.create(
            engine="text-davinci-002",
            prompt=text,
            max_tokens=max_summary_tokens,
            temperature=TEMPERATURE,
            top_p=1,
            frequency_penalty=0,
            presence_penalty=0.6,
            stop=["\n", " Human:", " AI:"]
        )
        summaries.append(response.choices[0].text)
    summary = " ".join(summaries)
    return summary


def update_video_table(videoUrl, properties) -> int:
    try:
        videoId = vidIdPattern.search(videoUrl).group()
        item = {
            "VideoId": {"S": videoId},
            "VideoUrl": {"S": videoUrl}
        }
        if properties is None:
            dyndb.put_item(
                TableName="videos",
                Item=item
            )
            return 0

        if type(properties) is dict:
            properties = [properties]
        attributeValues = {}
        updaetExpression = "SET "
        for property in properties:
            if property:
                attributeValues[f":{property['name']}"] = property['value']
                updaetExpression += f"{property['name']} = :{property['name']}" if updaetExpression == "SET " else f", {property['name']} = :{property['name']}"

        dyndb.update_item(
            Key={
                "VideoId": {"S": videoId}
            },
            TableName="videos",
            ExpressionAttributeValues=attributeValues,
            UpdateExpression=updaetExpression,
            ReturnValues="UPDATED_NEW"
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
        thumbnail_url = yt.thumbnail_url
        keywords = yt.keywords
        use_oauth = yt.use_oauth
        captions = yt.captions['a.en'].xml_captions if 'a.en' in yt.captions else ''
        return captions, description, thumbnail_url, keywords, use_oauth
    except Exception as e:
        logger.error("GET_CAPTION_error")
        logger.error(e)
        return captions, description, thumbnail_url, keywords, use_oauth

def xml_to_csv(xml):
    if(len(xml) < 30):
        return ""
    caption_obj = xmltodict.parse(xml)['timedtext']['body']['p']
    captions_csv = ""
    captions_txt = ""
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
        captions_txt += (f"{text} ")
    return captions_csv, captions_txt

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
    if('videoUrl' in event): # Lambda test invoke
        videoUrl = event['videoUrl']
    elif('queryStringParameters' in event and event['queryStringParameters'] and 'videoUrl' in event['queryStringParameters']): # GET request with query string or API Gateway proxy tester
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
    xml_captions, description, thumbnail_url, keywords, use_oauth = get_captions(videoUrl)
    logger.info("WRITING XML and DESCRIPTION TO S3")

    if len(xml_captions) > 30:
        captionFile = f"/{videoId}/captions.xml"
        if not is_head_object(Bucket=BUCKET_NAME, Key=captionFile):
            S3.put_object(Bucket=BUCKET_NAME, Key=captionFile, Body=xml_captions)

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
            },
            {
                "name": "thumbnail_url",
                "value": {"S": thumbnail_url}
            },
            {
                "name": "keywords",
                "value": {"S": keywords}
            },
            {
                "name": "use_oauth",
                "value": {"S": use_oauth}
            }
        ]
    )

    if len(xml_captions) > 30:
        logger.info("## CCONVERT XML CAPTIONS TO CSV AND WRITING CSV")
        csv_captions, txt_captions = xml_to_csv(xml_captions)
        captionFile = f"/{videoId}/captions.csv"
        summaryFile = f"/{videoId}/summary.txt"

        if not is_head_object(Bucket=BUCKET_NAME, Key=captionFile):
            S3.put_object(Bucket=BUCKET_NAME, Key=captionFile, Body=csv_captions)
        if not is_head_object(Bucket=BUCKET_NAME, Key=summaryFile):
            summary_txt = get_summary(text=txt_captions)
            if summary_txt and len(summary_txt) > 0:
                S3.put_object(Bucket=BUCKET_NAME, Key=summaryFile, Body=summary_txt)

        update_video_table(videoUrl, {"name": "csv_captions", "value": {"S": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}})

    # Get Video
    logger.info("## GETTING VIDEO AND WRITING Video File")
    videoFile = f"/{videoId}/videos.mp4"

    # Check if video already exists
    if is_head_object(Bucket=BUCKET_NAME, Key=videoFile):
        videoDataResult = Result(statusCode=200, message="Video already exists")
    else:
        # GET Video
        videoDataResult = get_video(videoUrl)
        if videoDataResult and videoDataResult.statusCode == 200:
            videoData = videoDataResult.body
            # Put Video
            S3.put_object(Bucket=BUCKET_NAME, Key=videoFile, Body=videoData)

    presigned_url = S3.generate_presigned_url('get_object', Params={'Bucket': BUCKET_NAME, 'Key': videoFile}, ExpiresIn=URL_EXPIRATION)

    # Update Video Table
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
        "body": videoDataResult.message + "\nPresigned URL: " + presigned_url if videoDataResult and videoDataResult.statusCode == 200 else  videoDataResult.error if videoDataResult else "Error getting video"
   }

if len(sys.argv) > 1:
    if os.path.exists('events/event.json'):
        f = open("events/event.json", "r")
        eventString = f.read()
        f.close()
        lambda_handler(json.loads(eventString), None)
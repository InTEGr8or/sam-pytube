import json
import io
from datetime import datetime
from pytube import YouTube
from boto3 import Session
import sys
import logging
import os.path
import re
import xmltodict
from functools import reduce
import openai

session = Session(region_name="us-east-1")
S3 = session.client('s3')
dyndb = session.client('dynamodb')
ssm = session.client('ssm')

configs = ssm.get_parameter(Name="/civilton/youtube/config", WithDecryption=True)['Parameter']['Value'].split(",")

BUCKET_NAME = [config for config in configs if 'bucket_name' in config][0].split("=")[1]
URL_EXPIRATION = [config for config in configs if 'url_expiration' in config][0].split("=")[1]
CHAT_GPT_TOKEN = [config for config in configs if 'CHAT_GPT_TOKEN' in config][0].split("=")[1]
MAX_TOKENS = int([config for config in configs if 'max_tokens' in config][0].split("=")[1])
TEMPERATURE = float([config for config in configs if 'temperature' in config][0].split("=")[1])
ENGINE = [config for config in configs if 'engine' in config][0].split("=")[1]

MAX_REQUEST_TOKENS = 4000

openai.api_key = CHAT_GPT_TOKEN

# Logger writes to CloudWatch
logger = logging.getLogger()
logger.setLevel(logging.INFO)

vidIdPattern = re.compile(r"((?<=watch\?v=).*|(?<=.be\/)(.*))|(?<=shorts\/)(.*)(?=(\?|/))?")

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

def split_text(text: str, max_chars: int=2049) -> list:
    """Splits a string, keeping words together.

    Args:
        text: The string to split.
        max_chars: The maximum number of characters per string (default 2049).
    Returns:
        A list of strings, each with a maximum of max_chars characters.
    """
    words = text.split()
    groups = []
    current_group = ""

    for word in words:
        if len(current_group) < max_chars:
            current_group += " " + word
        else:
            groups.append(current_group[1:])
            current_group = " " + word

    return groups

def get_summary_stream(text: str, max_tokens, temperature=0.9, engine="davinci") -> str:
    """
    Use the openai.Completion.stream() method to stream the text to the GPT-3 model.
    """
    completion = openai.Completion.stream(
        model=engine,
        temperature=temperature, # controls the creativity of the summary
        max_tokens=max_tokens, # maximum number of tokens in the summary
    )

    """
    Send the text to the GPT-3 model in chunks of 2049 tokens or fewer.
    """
    text = f"Split into sentences & summarize the following text in {max_tokens} tokens: \n\n[{text}]"
    for i in range(0, len(text), MAX_REQUEST_TOKENS):
        chunk = text[i:i+MAX_REQUEST_TOKENS]
        completion.add_input(prompt=chunk)

    """
    Get the summary from the GPT-3 model.
    """
    summary = completion.get_response()["choices"][0]["text"]

def get_summary_response(text: str, max_tokens, temperature=0.9, engine="davinci") -> str:
    # TODO: WRONG! Summary must be split by words, or better yet, sentences

    # If the text is too long, split it into chunks and summarize each chunk
    # 83 is the length of the prompt
    chunks = split_text(text, max_chars=(MAX_REQUEST_TOKENS - 83))
    # If the text is too short, return an empty string
    # Prevents divide by zero error
    if len(chunks) == 0:
        return ""
    max_summary_tokens = int(max_tokens / len(chunks))
    summaries = []
    for chunk in chunks:
        prompt_chunk = f"Split into sentences & summarize the following text in {max_summary_tokens} tokens: \n\n[{chunk}]"
        response = openai.Completion.create(
            engine=engine,
            prompt=prompt_chunk,
            max_tokens=max_summary_tokens,
            temperature=temperature,
            top_p=1
        )
        print("RESPONSE")
        print(response)
        summaries.append(response.choices[0].text)
    summary = " ".join(summaries)
    return summary

def get_summary(text: str, max_tokens, temperature=0.9, engine="davinci") -> str:
    if len(text) > 1000000000: #MAX_REQUEST_TOKENS:
        return get_summary_stream(text, max_tokens, temperature, engine)
    else:
        return get_summary_response(text, max_tokens, temperature, engine)


def update_video_table(videoUrl, properties) -> int:
    videoId = vidIdPattern.search(videoUrl).group()
    item = {
        "VideoId": {"S": videoId},
        "VideoUrl": {"S": videoUrl}
    }
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
    except Exception as e:
        logger.error('put_video_table ERROR')
        logger.error(e)
        logger.error('VIDEO_URL')
        logger.error(dir(videoUrl))
        return 1

    try:
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
        logger.error(attributeValues)
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
        title = yt.title
        description = yt.description
        thumbnail_url = yt.thumbnail_url
        keywords = yt.keywords
        use_oauth = yt.use_oauth
        captions = yt.captions['a.en'].xml_captions if 'a.en' in yt.captions else ''
        return captions, description, thumbnail_url, keywords, use_oauth, title
    except Exception as e:
        logger.error("GET_CAPTION_error")
        logger.error(e)
        return captions, description, thumbnail_url, keywords, use_oauth, title

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
    except Exception as e:
        print(e)
        return False

def get_audio (videoUrl) -> Result:
    try:
        yt = YouTube(videoUrl).streams
        audio_streams = list(filter(lambda fs:fs.type=='audio', yt.fmt_streams))
        audio_sorted = list(sorted(audio_streams, key=lambda au:au.filesize, reverse=True))
        audio_stream = audio_sorted[2]
    except Exception as e:
        logger.error("GET_AUDIO_ERROR")
        logger.error(e)
        return Result(500, error="Error getting audio")

    buf = io.BytesIO()
    try:
        audio_stream.stream_to_buffer(buf)
        buf.seek(0)
        audioData = buf.read()
        return Result(200, message="Total bytes saved: " + str(len(audioData)), body=audioData) 
    except Exception as e:
        logger.info("GET_AUDIO_ERROR - Buffer read error")
        logger.error(e)
        return Result(500, error="Buffer read error")

def get_video (videoUrl) -> Result:
    try:
        yt = YouTube(videoUrl).streams.filter(
        progressive=True, subtype='mp4',
    ).order_by('filesize').desc().first()
    except Exception as e:
        logger.error("GET_VIDEO_ERROR")
        logger.error(e)
        return Result(500, error="Error getting video")
    logger.info("GET_VIDEO")
    logger.info(f"FileSIZE: {yt.filesize}")
    buf = io.BytesIO()
    try:
        yt.stream_to_buffer(buf)
        buf.seek(0)
        videoData = buf.read()
        return Result(200, message="Total bytes saved: " + str(len(videoData)), body=videoData) 
    except Exception as e:
        logger.info("GET_VIDEO_ERROR - Buffer read error")
        logger.error(e)
        return Result(500, error="Buffer read error", message="Video file size: " + str(yt.filesize))

def lambda_handler(event, context):
    logger.info("HANDLER EVENT:")
    logger.info(event)

    # Get Video URL
    # The videoUrl will be in different paths in the event jsonpath depending on how the lambda is invoked
    replace_summary = True if('replaceSummary' in event and event['replaceSummary'] == 'true') else False
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

    # Get Captions and Description and metadata
    logger.info("GETTING XML CAPTIONS AND DESCRIPTION")
    xml_captions, description, thumbnail_url, keywords, use_oauth, title = get_captions(videoUrl)
    logger.info("WRITING XML and DESCRIPTION TO S3")

    # Write captions to S3
    if len(xml_captions) > 30:
        captionFile = f"{videoId}/{videoId}_captions.xml"
        if not is_head_object(Bucket=BUCKET_NAME, Key=captionFile):
            S3.put_object(Bucket=BUCKET_NAME, Key=captionFile, Body=xml_captions)

    # Write description and metadata to DynamoDB
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
                "value": {"S": ",".join(keywords) if keywords.__class__ == list else keywords}
            },
            {
                "name": "use_oauth",
                "value": {"BOOL": use_oauth}
            }
        ]
    )

    # Create CSV captions and summary
    summary = ""
    txt_captions = ""
    if len(xml_captions) > 30:
        logger.info("## CCONVERT XML CAPTIONS TO CSV AND WRITING CSV")
        csv_captions, txt_captions = xml_to_csv(xml_captions)
        captionFile = f"{videoId}/{videoId}_captions.csv"
        summaryFile = f"{videoId}/{videoId}_summary.txt"

        if not is_head_object(Bucket=BUCKET_NAME, Key=captionFile):
            S3.put_object(Bucket=BUCKET_NAME, Key=captionFile, Body=csv_captions)

        # Create summary or get summary from S3
        if is_head_object(Bucket=BUCKET_NAME, Key=summaryFile) and not replace_summary:
            summary = S3.get_object(Bucket=BUCKET_NAME, Key=summaryFile)['Body'].read().decode('utf-8')
        else:
            summary = get_summary(text=txt_captions, max_tokens=MAX_TOKENS, temperature=TEMPERATURE, engine=ENGINE)
            if summary and len(summary) > 0:
                S3.put_object(Bucket=BUCKET_NAME, Key=summaryFile, Body=summary)

        update_video_table(videoUrl, {"name": "csv_captions", "value": {"S": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}})

    # Get Video
    logger.info("## GETTING VIDEO AND WRITING Video File")
    videoFile = f"{videoId}/{videoId}_videos.mp4"

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

    video_presigned_url = S3.generate_presigned_url('get_object', Params={'Bucket': BUCKET_NAME, 'Key': videoFile}, ExpiresIn=URL_EXPIRATION)

    # Get audio
    logger.info("## GETTING audio AND WRITING audio File")
    audioFile = f"{videoId}/{videoId}_audio.mp3"

    # Check if audio already exists
    if is_head_object(Bucket=BUCKET_NAME, Key=audioFile):
        audioDataResult = Result(statusCode=200, message="audio already exists")
    else:
        # GET audio
        audioDataResult = get_audio(videoUrl)
        if audioDataResult and audioDataResult.statusCode == 200:
            audioData = audioDataResult.body
            # Put audio
            S3.put_object(Bucket=BUCKET_NAME, Key=audioFile, Body=audioData)

    audio_presigned_url = S3.generate_presigned_url('get_object', Params={'Bucket': BUCKET_NAME, 'Key': audioFile}, ExpiresIn=URL_EXPIRATION)
    
    # Update Video Table
    update_video_table(videoUrl, [
        {
            "name": "video_file", 
            "value": {"S": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        },
        {
            "name": "video_status",
            "value": {"S": str(videoDataResult.statusCode) if videoDataResult else "400"}
        }
    ])

    return {
        "statusCode": 200 if videoDataResult and videoDataResult.statusCode == 200 else 400,
        "body": '''{message}
        Presigned VIDEO URL: {video_presigned_url}
        Presigned AUDIO URL: {audio_presigned_url}
        SUMMARY OF VIDEO: {summary}
        '''.format(message=videoDataResult.message, video_presigned_url=video_presigned_url, audio_presigned_url=audio_presigned_url, summary=summary) if videoDataResult and videoDataResult.statusCode == 200 else  videoDataResult.error if videoDataResult else "Error getting video"
   }

if len(sys.argv) > 1:
    if os.path.exists('events/event.json'):
        f = open("events/event.json", "r")
        eventString = f.read()
        f.close()
        lambda_handler(json.loads(eventString), None)
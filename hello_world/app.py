import json
from pytube import YouTube
import boto3
import sys
import logging
import os.path
import re
# import xmltodict

S3 = boto3.client('s3', region_name="us-east-1")
vidIdPattern = re.compile(r"((?<=watch\?v=).*|(?<=.be\/)(.*))")

def get_captions (videoUrl):
    try:
        captions = YouTube(videoUrl).captions['a.en']
        return captions.xml_captions
    except Exception as e:
        logger.error(dir(videoUrl), e)
        return 1

def get_video (videoUrl):
    try:
        yt = YouTube(videoUrl).streams.filter(
        progressive=True, subtype='mp4',
    ).order_by('filesize').desc().first()
    except Exception as e:
        logger.error(dir(videoUrl), e)
        return

    dl_stream = yt
    try:
        captions = dl_stream.player_config_args['player_response'][
            'captions']['playerCaptionsTracklistRenderer']['captionTracks']
        english_captions = next(
            (c for c in captions if c['languageCode'] == 'en'), None)
        englishCaptionsUrl = english_captions['baseUrl']
        file = urllib.request.urlopen(englishCaptionsUrl)
        data = file.read()
        file.close()
        print("CAPTIONS FOR:", caption_path)

        data = xmltodict.parse(data)
        if data:

            transcript = '\n'.join(line['@start'] + ': ' + line['#text'] for line in data["transcript"]["text"])
            file_stream = open(caption_path, "w")
            file_stream.close()
        return 0
    except Exception as e:
        logger.info("NO CAPTIONS\n")



def lambda_handler(event, context):
    """Sample pure Lambda function

    Parameters
    ----------
    event: dict, required
        API Gateway Lambda Proxy Input Format

        Event doc: https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-lambda-proxy-integrations.html#api-gateway-simple-proxy-for-lambda-input-format

    context: object, required
        Lambda Context runtime methods and attributes

        Context doc: https://docs.aws.amazon.com/lambda/latest/dg/python-context-object.html

    Returns
    ------
    API Gateway Lambda Proxy Output Format: dict

        Return doc: https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-lambda-proxy-integrations.html
    """

    # try:
    #     ip = requests.get("http://checkip.amazonaws.com/")
    # except requests.RequestException as e:
    #     # Send some context about this error to Lambda Logs
    #     print(e)

    #     raise e
    videoUrl = event['videoUrl']
    videoId = vidIdPattern.search(videoUrl).group()
    xml_captions = get_captions(videoUrl)
    captionFile = f"captions/{videoId}.xml"
    S3.put_object(Bucket="civilton-youtube-content", Key=captionFile, Body=xml_captions)
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "hello world",
            # "location": ip.text.replace("\n", "")
        }),
    }

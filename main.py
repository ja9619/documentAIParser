import logging
import os
import uuid
import zipfile
import json
from typing import Any

import xlsxwriter
from google.cloud import documentai
import form_keys
from utils import layout_to_text, BASE_DIR, FORM_15G, FORM_15H
from google.api_core.client_options import ClientOptions

logger = logging.getLogger(__name__)
config_data = Any


def online_process(file_content: any, mime_type: str) -> documentai.Document:
    """
    Processes a document using the Document AI Online Processing API.
    """
    location = config_data['credentials']['location']
    project_id = config_data['credentials']['project_id']
    processor_id = config_data['credentials']['processor_id']

    # You must set the api_endpoint if you use a location other than 'us'.
    opts = ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")

    # Instantiates a client
    client = documentai.DocumentProcessorServiceClient(client_options=opts)

    # The full resource name of the processor, e.g.:
    # projects/project_id/locations/location/processor/processor_id
    name = client.processor_path(project_id, location, processor_id)

    # Load Binary Data into Document AI RawDocument Object
    raw_document = documentai.RawDocument(content=file_content, mime_type=mime_type)

    # Configure the process request
    request = documentai.ProcessRequest(name=name, raw_document=raw_document)

    # Use the Document AI client to process the sample form
    result = client.process_document(request=request)

    with open('results.txt', 'a') as f:
        f.write(str(result.document.pages))

    return result.document


def parse_document(content, mime_type):
    # find whether the document is 15G or 15H
    form_type = ""

    results = dict()
    count_keys = 0
    page_number = 1

    # function which calls our DocumentAI API
    document = online_process(file_content=content, mime_type=mime_type)
    if document is None:
        logger.error("received a null response from google cloud API")
        return None, ""

    text = document.text
    stop_processing = False
    keys_found = set()

    for page in document.pages:
        # for block in page.blocks:
        #     # extract the OCR text to determine the tax form type
        #     for paragraph in block.paragraphs:
        #         for word in paragraph.words:
        #             if FORM_15G.lower() in word.lower():
        #                 form_type = FORM_15G
        #                 break
        #             elif FORM_15H.lower() in word.lower():
        #                 form_type = FORM_15H
        #                 break
        #             elif form_type != "":
        #                 # found the form type, don't dig deeper
        #                 break

        if FORM_15G in text:
            form_type = FORM_15G
        elif FORM_15H in text:
            form_type = FORM_15H

        if form_type != "":
            logger.debug(f'tax document type found to be: {form_type}!')
        else:
            logger.warning("error cannot decide the tax document type.")
            # only try till second page to find the document type
            if page_number < 2:
                page_number += 1
                logger.debug("Going to the next page to find the document type")
                continue
            return None, ""

        # key value pairs
        if 'form_fields' in page:
            for field in page.form_fields:
                # Get the extracted field names
                name = layout_to_text(field.field_name, text, True)

                if "Signature" in name:
                    stop_processing = True
                    continue

                # Confidence - How "sure" the Model is that the text is correct
                name_confidence = field.field_name.confidence
                if name_confidence < config_data['acceptance']['key_threshold']:
                    continue

                values = layout_to_text(field.field_value, text, False)
                value_confidence = field.field_value.confidence
                if value_confidence < config_data['acceptance']['value_threshold']:
                    continue

                # handle checked keys
                if name.lower() in form_keys.checked_values_list:
                    if field.value_type == "filled_checkbox":
                        official_form_key = form_keys.get_checked_key(form_type)
                        results[official_form_key] = name
                    continue

                # get the official field in the form which corresponds to this field name
                official_form_key = form_keys.inspect_form_key(form_type, name, False, keys_found)
                if official_form_key is not None:
                    results[official_form_key] = values
                    count_keys += 1
                    keys_found.update(official_form_key)
                else:
                    logger.warning(f'key: {name} not found under form keys')
        else:
            logger.debug(f'no form keys data found on this page:{page_number}')

        # table data
        if "tables" in page:
            for index, table in enumerate(page.tables):
                header_cells = table.header_rows[0].cells
                for cell in header_cells:
                    cell_text = layout_to_text(cell.layout, text, True)
                    official_table_key = form_keys.inspect_form_key(form_type, cell_text, True, keys_found)
                    if official_table_key is not None:
                        count_keys += 1
                        # If a match is found, extract the corresponding column data
                        col_idx = header_cells.index(cell)
                        col_data = [layout_to_text(row.cells[col_idx].layout, text, False) for row in table.body_rows]
                        results[official_table_key] = col_data
                    else:
                        logger.debug(f'key: {cell_text} not found under table keys')
        else:
            logger.debug(f'no table data found on this page:{page_number}')

        page_number += 1
        if count_keys >= form_keys.get_max_keys_needed(form_type):
            logger.debug("found all the keys")
        elif page_number > 3 or stop_processing:
            logger.debug("ending the parse operation.")
            break

    logger.debug(f'found: {count_keys} keys from the document')
    return results, form_type


def process_tax_files(zip_file_path):
    # output excel path
    base_path = os.path.join(BASE_DIR, 'output')
    if not os.path.exists(base_path):
        os.makedirs(base_path)
    excel_file_name = f"{str(uuid.uuid4())}-results.xlsx"
    excel_file_path = os.path.join(base_path, excel_file_name)

    try:
        workbook = xlsxwriter.Workbook(excel_file_path)
    except FileNotFoundError as e:
        print(f"Error: {e}. The specified directory or file does not exist.")
        return

    worksheet = workbook.add_worksheet("Tax Data")

    # Add text wrapping format to the cells
    wrap_format = workbook.add_format({"text_wrap": True})
    worksheet.set_column("A:Z", None, wrap_format)
    row_number = 1

    headers_defined = False

    # loop through all files in the zip folder
    with zipfile.ZipFile(zip_file_path, 'r') as zip_file:
        for file_name in zip_file.namelist():
            with zip_file.open(file_name) as cur_file:
                file_content = cur_file.read()

                # ignore any non-PDF or jpg files
                if file_name.endswith(".pdf"):
                    mime_type = "application/pdf"
                elif file_name.endswith(".jpg"):
                    mime_type = "image/jpeg"
                else:
                    logger.warning(f'cannot find the mime type for the file: {file_name}')
                    continue

                response_dict, form_type = parse_document(file_content, mime_type)
                if response_dict is None:
                    logger.error(f'could not get values for the file:{file_name}')
                    continue

                # Add headers to the worksheet once
                if not headers_defined:
                    headers = form_keys.get_all_keys(form_type)
                    if headers is None:
                        logger.error(f'cannot make the excel sheet headers for file: {file_name}')
                        continue

                    for i, header in enumerate(headers):
                        worksheet.write(0, i, header)
                    headers_defined = True

                # Write the dictionary values to the sheet
                for key, value in response_dict.items():
                    # Find the column index for the key
                    try:
                        col_index = headers.index(key)
                    except ValueError:
                        logger.error(f'cannot find the entry in form keys for column:{key}')
                        continue  # Skip keys not found in columns

                    # Write the value to the cell in the corresponding row and column
                    if isinstance(value, list):
                        new_value = ', '.join([str(elem) for elem in value if elem != ""])
                    else:
                        new_value = value

                    worksheet.write(row_number, col_index, new_value)
                row_number += 1

    workbook.close()
    logger.debug(f'finished processing of the tax files under: {zip_file_path}')


if __name__ == '__main__':
    with open("config.json") as json_data_file:
        config_data = json.load(json_data_file)

    for file in config_data['files']:
        process_tax_files(os.path.join(BASE_DIR, file))

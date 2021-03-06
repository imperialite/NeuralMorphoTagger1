import sys

def descr_to_feats(symbol, return_dict=False):
    if "," in symbol:
        symbol, feats = symbol.split(",", maxsplit=1)
        fields = []
        for elem in feats.split("|"):
            key, values = elem.split("=", maxsplit=1)
            values = values.split(",")
            fields.extend((key, value) for value in values)
            # fields.append((key, values))
        fields = tuple(fields)
    else:
        fields = ()
    if return_dict:
        fields = dict(fields)
    return symbol, fields



def is_subsumed(first_tag, second_tag):
    """
    Checks whether all features of first_tag are present in second_tag
    """
    first_pos, first_descr = descr_to_feats(first_tag, return_dict=True)
    second_pos, second_descr = descr_to_feats(second_tag, return_dict=True)
    if first_pos != second_pos:
        return False
    for key, value in first_descr.items():
        if key == "Abbr":
            continue
        if value == "Ptan":
            value = "Plur"
        if value == "Brev":
            value = "Short"
        if second_descr.get(key) != value:
            return False
    return True


def read_tags_input(infile):
    answer, curr_sent = [], []
    with open(infile, "r", encoding="utf8") as fin:
        for line in fin:
            line = line.strip()
            if line == "":
                if len(curr_sent) > 0:
                    answer.append([curr_sent])
                curr_sent = []
                continue
            curr_sent.append(line)
        if len(curr_sent) > 0:
            answer.append([curr_sent])
    return answer


